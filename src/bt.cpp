#include "bt.hpp"
#include "proxy.hpp"
#include <libtorrent/settings_pack.hpp>
#include <libtorrent/session_params.hpp>
#include <libtorrent/alert_types.hpp>
#include <libtorrent/bdecode.hpp>
#include <libtorrent/read_resume_data.hpp>
#include <libtorrent/write_resume_data.hpp>
#include <libtorrent/torrent_flags.hpp>
#include <sodium.h>
#include <algorithm>
#include <chrono>
#include <fstream>
#include <fcntl.h>
#include <regex>
#include <unistd.h>
namespace lv {
namespace lt=libtorrent;
namespace {
std::string hex(const std::string& bytes) {
    std::string out(bytes.size()*2+1,'\0'); sodium_bin2hex(out.data(),out.size(),reinterpret_cast<const unsigned char*>(bytes.data()),bytes.size()); out.pop_back(); return out;
}
std::string unhex(const std::string& text) {
    if (text.size()>16*1024*1024 || text.size()%2) throw Error(500,"invalid saved BT state");
    std::string out(text.size()/2,'\0');
    if (sodium_hex2bin(reinterpret_cast<unsigned char*>(out.data()),out.size(),text.data(),text.size(),nullptr,nullptr,nullptr)!=0) throw Error(500,"invalid saved BT state");
    return out;
}
std::string read_metadata(const fs::path& path) {
    auto file=regular_file(directory(path.parent_path()),path.filename()); auto length=file->stat().st_size;
    if (length<1 || length>1024*1024) throw Error(400,"torrent metadata must be 1..1048576 bytes");
    std::string bytes(static_cast<size_t>(length),'\0'); if (::pread(file->fd,bytes.data(),bytes.size(),0)!=length) throw Error(500,"cannot read BT metadata"); return bytes;
}
void component(const std::string& text) {
    if (text.empty() || text.size()>200 || text=="." || text==".." || text.front()=='.' || text.find_first_of("/\\:")!=std::string::npos || text.find('\0')!=std::string::npos || std::any_of(text.begin(),text.end(),[](unsigned char c){return c<32 || c==127;})) throw Error(400,"unsafe torrent path component");
    try { (void)Json(text).dump(); } catch (...) { throw Error(400,"torrent path must be UTF-8"); }
}
void unique_keys(const lt::bdecode_node& node) {
    if (node.type()==lt::bdecode_node::dict_t) {
        std::set<std::string> keys;
        for (int i=0;i<node.dict_size();++i) {
            auto [key,value]=node.dict_at(i); if (!keys.insert(std::string(key)).second) throw Error(400,"duplicate torrent metadata key"); unique_keys(value);
        }
    } else if (node.type()==lt::bdecode_node::list_t) for (int i=0;i<node.list_size();++i) unique_keys(node.list_at(i));
}
lt::settings_pack settings(const Json& proxy) {
    lt::settings_pack s;
    s.set_str(lt::settings_pack::listen_interfaces,"0.0.0.0:0");
    s.set_bool(lt::settings_pack::enable_dht,false); s.set_bool(lt::settings_pack::enable_lsd,false);
    s.set_bool(lt::settings_pack::enable_upnp,false); s.set_bool(lt::settings_pack::enable_natpmp,false);
    s.set_bool(lt::settings_pack::enable_outgoing_utp,false); s.set_bool(lt::settings_pack::enable_incoming_utp,false);
    s.set_bool(lt::settings_pack::validate_https_trackers,true);
    s.set_int(lt::settings_pack::connections_limit,128);
    s.set_int(lt::settings_pack::min_reconnect_time,5);
    s.set_int(lt::settings_pack::alert_mask,static_cast<int>(lt::alert_category::error|lt::alert_category::storage|lt::alert_category::status|lt::alert_category::tracker));
    auto seconds=std::max(1,(proxy["timeout_ms"].get<int>()+999)/1000);
    s.set_int(lt::settings_pack::peer_connect_timeout,seconds);
    s.set_int(lt::settings_pack::tracker_receive_timeout,seconds);
    auto type=proxy["type"].get<std::string>(); bool auth=!proxy["username"].get<std::string>().empty();
    int kind=lt::settings_pack::none;
    if (type=="socks5") kind=auth?lt::settings_pack::socks5_pw:lt::settings_pack::socks5;
    if (type=="http") kind=auth?lt::settings_pack::http_pw:lt::settings_pack::http;
    s.set_int(lt::settings_pack::proxy_type,kind); s.set_str(lt::settings_pack::proxy_hostname,proxy["host"].get<std::string>());
    s.set_int(lt::settings_pack::proxy_port,proxy["port"].get<int>());
    s.set_str(lt::settings_pack::proxy_username,proxy["username"].get<std::string>()); s.set_str(lt::settings_pack::proxy_password,proxy["password"].get<std::string>());
    s.set_bool(lt::settings_pack::proxy_hostnames,true); s.set_bool(lt::settings_pack::proxy_peer_connections,true); s.set_bool(lt::settings_pack::proxy_tracker_connections,true);
    return s;
}
}
std::string Bt::root() { return db_.query("SELECT value FROM settings WHERE key='download_root'")[0]["value"].get<std::string>(); }
std::string Bt::encrypt(const Json& input) {
    auto plain=input.dump(); std::string bytes(crypto_secretbox_NONCEBYTES+crypto_secretbox_MACBYTES+plain.size(),'\0');
    auto* buffer=reinterpret_cast<unsigned char*>(bytes.data()); randombytes_buf(buffer,crypto_secretbox_NONCEBYTES);
    crypto_secretbox_easy(buffer+crypto_secretbox_NONCEBYTES,reinterpret_cast<const unsigned char*>(plain.data()),plain.size(),buffer,key_.data()); return hex(bytes);
}
Json Bt::decrypt(const std::string& input) {
    auto bytes=unhex(input); if (bytes.size()<crypto_secretbox_NONCEBYTES+crypto_secretbox_MACBYTES) throw Error(500,"encrypted proxy settings damaged");
    std::string plain(bytes.size()-crypto_secretbox_NONCEBYTES-crypto_secretbox_MACBYTES,'\0'); auto* buffer=reinterpret_cast<const unsigned char*>(bytes.data());
    if (crypto_secretbox_open_easy(reinterpret_cast<unsigned char*>(plain.data()),buffer+crypto_secretbox_NONCEBYTES,bytes.size()-crypto_secretbox_NONCEBYTES,buffer,key_.data())!=0) throw Error(500,"cannot decrypt proxy settings with local key");
    return validate_proxy(Json::parse(plain));
}
Bt::Bt(Database& db,Auth& auth,const Config& config):db_(db),auth_(auth),config_(config) {
    db_.query("CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,owner INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,root TEXT NOT NULL,name TEXT NOT NULL,info_hash TEXT NOT NULL UNIQUE,paused INTEGER NOT NULL DEFAULT 1,resume TEXT NOT NULL DEFAULT '',created INTEGER NOT NULL)");
    db_.query("CREATE TABLE IF NOT EXISTS bt_history(user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,file_index INTEGER NOT NULL,position REAL NOT NULL,updated INTEGER NOT NULL,PRIMARY KEY(user_id,task_id,file_index))");
    auto default_root=config.data/"downloads"; (void)private_directory(default_root);
    db_.query("INSERT OR IGNORE INTO settings(key,value) VALUES('download_root',?)",{default_root.string()}); (void)directory(root());
    (void)private_directory(config.data/"bt-meta");
    auto folder=directory(config.data);
    int fd=::openat(folder->fd,"proxy.key",O_CREAT|O_EXCL|O_RDWR|O_CLOEXEC|O_NOFOLLOW,0600);
    bool created=fd>=0;
    if (!created && errno==EEXIST) fd=::openat(folder->fd,"proxy.key",O_RDONLY|O_CLOEXEC|O_NOFOLLOW|O_NONBLOCK);
    if (fd<0) throw Error(500,"cannot open local proxy encryption key");
    auto keyfile=std::make_shared<File>(fd); auto stat=keyfile->stat();
    if (!S_ISREG(stat.st_mode) || stat.st_nlink!=1 || (stat.st_mode&077)!=0 || stat.st_uid!=::geteuid()) throw Error(500,"proxy key must be an owned private regular file without links");
    if (created) {
        randombytes_buf(key_.data(),key_.size());
        if (::write(fd,key_.data(),key_.size())!=static_cast<ssize_t>(key_.size()) || ::fsync(fd)!=0 || ::fsync(folder->fd)!=0) throw Error(500,"cannot persist proxy encryption key");
    } else if (stat.st_size!=32 || ::read(fd,key_.data(),key_.size())!=32) throw Error(500,"invalid local proxy encryption key");
    auto stored=db_.query("SELECT value FROM settings WHERE key='bt_proxy'");
    proxy_=stored.empty()?validate_proxy(Json::object()):decrypt(stored[0]["value"].get<std::string>());
    session_=std::make_unique<lt::session>(lt::session_params(settings(proxy_)));
    for (const auto& row:db_.query("SELECT * FROM tasks")) {
        auto id=row["id"].get<long long>();
        try { auto info=validate(read_metadata(config.data/"bt-meta"/(std::to_string(id)+".torrent"))); add(row,info); }
        catch (...) { faults_[id]={{"category","restore"},{"code",1},{"message","task restore failed; check metadata and protected download directory"}}; }
    }
    worker_=std::jthread([this](std::stop_token token) {
        auto last=std::chrono::steady_clock::now();
        while (!token.stop_requested()) {
            try {
                std::lock_guard lock(mutex_); drain(); schedule();
                if (std::chrono::steady_clock::now()-last>std::chrono::seconds(5)) {
                    std::vector<long long> removed;
                    for (const auto& [id,handle]:handles_) {
                        auto rows=db_.query("SELECT t.id,u.disabled FROM tasks t JOIN users u ON u.id=t.owner WHERE t.id=?",{id});
                        if (rows.empty()) { handle.pause(); removed.push_back(id); continue; }
                        if (rows[0]["disabled"]==1) { handle.pause(); db_.query("UPDATE tasks SET paused=1 WHERE id=?",{id}); }
                        save(id);
                    }
                    for (auto id:removed) { forget_stream(id); session_->remove_torrent(handles_.at(id)); handles_.erase(id); saving_.erase(id); faults_.erase(id); }
                    last=std::chrono::steady_clock::now();
                }
            } catch (...) { log("bt_worker_error",Json::object(),"error"); }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
    });
}
Bt::~Bt() {
    stop_streams();
    worker_.request_stop(); if (worker_.joinable()) worker_.join();
    try {
        std::lock_guard lock(mutex_);
        for (const auto& [id,handle]:handles_) { handle.pause(); save(id); }
        auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(3);
        while (!saving_.empty() && std::chrono::steady_clock::now()<deadline) { drain(); std::this_thread::sleep_for(std::chrono::milliseconds(20)); }
        if (!saving_.empty()) log("bt_resume_save_timeout",{{"tasks",saving_.size()}},"error");
    } catch (...) { log("bt_shutdown_error",Json::object(),"error"); }
    session_.reset(); sodium_memzero(key_.data(),key_.size());
}
std::shared_ptr<lt::torrent_info> Bt::validate(const std::string& bytes) {
    if (bytes.empty() || bytes.size()>1024*1024) throw Error(413,"torrent metadata exceeds 1 MiB");
    lt::error_code error; auto tree=lt::bdecode(lt::span<const char>(bytes.data(),bytes.size()),error,nullptr,32,100000);
    if (error || tree.type()!=lt::bdecode_node::dict_t || tree.data_section().size()!=static_cast<ptrdiff_t>(bytes.size())) throw Error(400,"invalid or trailing torrent metadata");
    unique_keys(tree);
    if (tree.dict_find("url-list") || tree.dict_find("httpseeds")) throw Error(400,"Web Seed unsupported in this version");
    auto info=tree.dict_find_dict("info");
    if (!info || info.dict_find("meta version") || !info.dict_find_string("pieces")) throw Error(400,"this version accepts v1 .torrent metadata only");
    for (const auto* key:{"name","name.utf-8"}) if (auto value=info.dict_find_string(key)) component(std::string(value.string_value()));
    auto files=info.dict_find_list("files");
    if (files && (files.list_size()<1 || files.list_size()>1000)) throw Error(400,"torrent file count must be 1..1000");
    if (files) for (int i=0;i<files.list_size();++i) {
        auto file=files.list_at(i); if (file.type()!=lt::bdecode_node::dict_t) throw Error(400,"invalid torrent file entry");
        if (file.dict_find("symlink path") || std::string(file.dict_find_string_value("attr")).find('l')!=std::string::npos) throw Error(400,"torrent symlinks unsupported");
        for (const auto* key:{"path","path.utf-8"}) {
            auto path=file.dict_find_list(key); if (!path && std::string(key)=="path") throw Error(400,"torrent path list required");
            if (path && (path.list_size()<1 || path.list_size()>16)) throw Error(400,"torrent path depth exceeded");
            if (path) for (int j=0;j<path.list_size();++j) { auto part=path.list_at(j); if (part.type()!=lt::bdecode_node::string_t) throw Error(400,"invalid torrent path"); component(std::string(part.string_value())); }
        }
    }
    if (info.dict_find("symlink path") || std::string(info.dict_find_string_value("attr")).find('l')!=std::string::npos) throw Error(400,"torrent symlinks unsupported");
    auto torrent=std::make_shared<lt::torrent_info>(tree,error); if (error) throw Error(400,"libtorrent rejected metadata");
    if (torrent->total_size()<1 || torrent->total_size()>100LL*1024*1024*1024 || torrent->num_pieces()>262144 || torrent->piece_length()<16384 || torrent->piece_length()>4*1024*1024 || torrent->files().num_files()>1000) throw Error(400,"torrent size or piece limits exceeded");
    std::set<std::string> paths;
    for (auto index:torrent->files().file_range()) {
        auto path=torrent->files().file_path(index); for (const auto& part:fs::path(path)) component(part.string());
        if (!paths.insert(path).second || (torrent->files().file_flags(index)&lt::file_storage::flag_symlink)) throw Error(400,"duplicate path or symlink in torrent");
    }
    if (torrent->trackers().empty() || torrent->trackers().size()>32) throw Error(400,"1..32 HTTP(S) trackers required; DHT and UDP trackers disabled");
    static const std::regex tracker(R"(^https?://[A-Za-z0-9.\-\[\]:]+(?:/[^\r\n]*)?$)");
    for (const auto& entry:torrent->trackers()) if (entry.url.size()>2048 || entry.url.find('\0')!=std::string::npos || !std::regex_match(entry.url,tracker)) throw Error(400,"tracker must be an HTTP(S) URL without userinfo");
    return torrent;
}
void Bt::add(const Json& row,const std::shared_ptr<lt::torrent_info>& info) {
    auto id=row["id"].get<long long>(); auto folder=fs::path(row["root"].get<std::string>())/("task-"+std::to_string(id));
    auto parent=directory(row["root"].get<std::string>());
    if (::mkdirat(parent->fd,folder.filename().c_str(),0700)!=0 && errno!=EEXIST) throw Error(507,"cannot create task download directory");
    auto base=directory(folder); auto stat=base->stat();
    if (stat.st_uid!=::geteuid() || (stat.st_mode&077)!=0) throw Error(400,"task directory must be owned and private");
    int marker=::openat(base->fd,".bt-task",O_WRONLY|O_CREAT|O_NOFOLLOW|O_CLOEXEC,0600);
    if (marker<0) throw Error(507,"cannot mark managed BT directory");
    auto marker_file=std::make_shared<File>(marker);
    if (!S_ISREG(marker_file->stat().st_mode) || ::fsync(marker)!=0 || ::fsync(base->fd)!=0) throw Error(507,"cannot sync BT directory marker");

    // Pre-create directories and reject links before handing paths to libtorrent.
    for (auto index:info->files().file_range()) {
        auto relative=fs::path(info->files().file_path(index)); auto current=base;
        auto leaf=relative.filename(); auto parts=relative.parent_path();
        for (const auto& part:parts) {
            if (::mkdirat(current->fd,part.c_str(),0700)!=0 && errno!=EEXIST) throw Error(507,"cannot create torrent subdirectory");
            int fd=::openat(current->fd,part.c_str(),O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC); if (fd<0) throw Error(400,"unsafe existing torrent directory"); current=std::make_shared<File>(fd);
        }
        struct stat existing{};
        if (::fstatat(current->fd,leaf.c_str(),&existing,AT_SYMLINK_NOFOLLOW)==0 && (!S_ISREG(existing.st_mode) || existing.st_nlink!=1)) throw Error(400,"unsafe existing torrent file");
    }
    lt::add_torrent_params params;
    if (!row["resume"].get<std::string>().empty()) {
        auto bytes=unhex(row["resume"].get<std::string>()); lt::error_code error;
        params=lt::read_resume_data(lt::span<const char>(bytes.data(),bytes.size()),error);
        if (error) { params=lt::add_torrent_params(); log("bt_resume_rejected",{{"task_id",id}}); }
    }
    params.ti=info; params.save_path=folder.string(); params.renamed_files.clear(); params.file_priorities.clear(); params.piece_priorities.clear();
    params.trackers.clear(); params.tracker_tiers.clear(); for (const auto& entry:info->trackers()) { params.trackers.push_back(entry.url); params.tracker_tiers.push_back(entry.tier); }
    params.url_seeds.clear(); params.http_seeds.clear();
    params.flags&=~(lt::torrent_flags::auto_managed|lt::torrent_flags::seed_mode|lt::torrent_flags::share_mode|lt::torrent_flags::upload_mode|lt::torrent_flags::sequential_download);
    if (row["paused"]==1) params.flags|=lt::torrent_flags::paused; else params.flags&=~lt::torrent_flags::paused;
    lt::error_code error; auto handle=session_->add_torrent(params,error); if (error) throw Error(409,"libtorrent could not add task");
    handle.set_max_connections(32); handles_[id]=handle;
    forget_stream(id); directories_[id]=base;
    // Rehash files on every restoration: persisted bitmap never authorizes unchecked bytes.
    if (!row["resume"].get<std::string>().empty()) handle.force_recheck();
}
void Bt::save(long long id) {
    if (saving_.contains(id) || !handles_.contains(id)) return;
    handles_.at(id).save_resume_data(lt::torrent_handle::flush_disk_cache); saving_.insert(id);
}
void Bt::drain() {
    std::vector<lt::alert*> alerts; session_->pop_alerts(&alerts);
    for (const auto* alert:alerts) {
        auto torrent=dynamic_cast<const lt::torrent_alert*>(alert); if (!torrent) continue;
        auto found=std::find_if(handles_.begin(),handles_.end(),[&](const auto& item){return item.second==torrent->handle;}); if (found==handles_.end()) continue;
        auto id=found->first;
        if (lt::alert_cast<lt::read_piece_alert>(alert)) { read_alert(alert,id); continue; }
        if (auto saved=lt::alert_cast<lt::save_resume_data_alert>(alert)) {
            auto bytes=lt::write_resume_data_buf(saved->params);
            if (bytes.size()<=8*1024*1024) db_.query("UPDATE tasks SET resume=? WHERE id=?",{hex(std::string(bytes.begin(),bytes.end())),id});
            saving_.erase(id);
        } else if (lt::alert_cast<lt::save_resume_data_failed_alert>(alert)) { saving_.erase(id); log("bt_resume_save_failed",{{"task_id",id}},"error"); }
        else if (auto bad=lt::alert_cast<lt::torrent_error_alert>(alert)) faults_[id]={{"category","torrent"},{"code",bad->error.value()},{"message","torrent storage or metadata error; inspect directory and retry"}};
        else if (auto tracker=lt::alert_cast<lt::tracker_error_alert>(alert)) faults_[id]={{"category","tracker"},{"domain",tracker->error.category().name()},{"code",tracker->error.value()},{"message","tracker connection failed; check tracker, proxy and timeout"}};
        else if (auto reply_alert=lt::alert_cast<lt::tracker_reply_alert>(alert)) { log("bt_tracker_reply",{{"task_id",id},{"peers_returned",reply_alert->num_peers}}); if (faults_.contains(id) && faults_[id]["category"]=="tracker") faults_.erase(id); }
        else if (lt::alert_cast<lt::torrent_checked_alert>(alert)) {
            log("bt_checked",{{"task_id",id}});
            auto rows=db_.query("SELECT paused FROM tasks WHERE id=?",{id});
            if (!rows.empty() && rows[0]["paused"]==0) torrent->handle.force_reannounce(0,-1,lt::torrent_handle::ignore_min_interval);
        }
        else if (auto damaged=lt::alert_cast<lt::hash_failed_alert>(alert)) {
            log("bt_piece_hash_failed",{{"task_id",id},{"piece",static_cast<int>(damaged->piece_index)}});
        }
        // Do not log libtorrent message(): it may contain tracker URLs or proxy credentials.
    }
}
Json Bt::owned(const httplib::Request& request,bool mutation) {
    auto who=auth_.require(request,false,mutation); long long id=0;
    try { id=std::stoll(request.matches[1].str()); } catch (...) { throw Error(400,"invalid task identifier"); }
    auto rows=db_.query("SELECT * FROM tasks WHERE id=? AND (?='admin' OR owner=?)",{id,who.role,who.id});
    if (rows.empty()) throw Error(404,"task not found for this account");
    return rows[0];
}
Json Bt::status(const Json& row) {
    auto id=row["id"].get<long long>(); Json result={{"id",id},{"owner",row["owner"]},{"name",row["name"]},{"created",row["created"]},{"paused",row["paused"]==1},{"source","libtorrent 2.0.11"},{"error",nullptr}};
    if (!handles_.contains(id)) { result["state"]="unavailable"; result["error"]=faults_.contains(id)?faults_[id]:Json({{"message","task unavailable"}}); return result; }
    auto handle=handles_.at(id); auto state=handle.status(lt::torrent_handle::query_pieces); auto info=handle.torrent_file();
    long long verified=0; int verified_pieces=0;
    bool checked_state=state.state==lt::torrent_status::downloading || state.state==lt::torrent_status::finished || state.state==lt::torrent_status::seeding;
    for (int i=0;checked_state && info && i<info->num_pieces();++i) if (i<state.pieces.size() && state.pieces[lt::piece_index_t(i)] && !quarantined_.contains({id,i})) { verified+=info->piece_size(lt::piece_index_t(i)); ++verified_pieces; }
    static const char* names[]={"queued","checking","metadata","downloading","finished","seeding","allocating","checking_resume"};
    auto index=static_cast<unsigned>(state.state);
    result["state"]=state.is_seeding?"seeding":(index<std::size(names)?names[index]:"unknown");
    result["paused_actual"]=static_cast<bool>(state.flags&lt::torrent_flags::paused);
    result["total_bytes"]=info?info->total_size():0; result["verified_bytes"]=verified;
    result["progress"]=info && info->total_size()?static_cast<double>(verified)/static_cast<double>(info->total_size()):0.0;
    result["download_bytes_per_second"]=state.download_payload_rate; result["peers"]=state.num_peers; result["seeds"]=state.num_seeds;
    result["piece_count"]=info?info->num_pieces():0; result["verified_pieces"]=verified_pieces;
    result["resume_saved"]=!row["resume"].get<std::string>().empty();
    if (faults_.contains(id)) result["error"]=faults_[id];
    if (state.errc) result["error"]={{"category","torrent"},{"code",state.errc.value()},{"message","torrent paused after storage or metadata error"}};
    return result;
}
void Bt::audit(const std::string& event,long long actor,long long task) {
    db_.query("INSERT INTO audit(timestamp,actor,event,detail) VALUES(?,?,?,?)",{now(),actor,event,Json({{"task_id",task}}).dump()}); log(event,{{"actor",actor},{"task_id",task}});
}
void Bt::preflight(const httplib::Request& request) {
    auth_.require(request,false,true);
    if (request.get_header_value("Content-Type")!="application/x-bittorrent") throw Error(415,"application/x-bittorrent required");
    if (request.has_header("Transfer-Encoding") || request.get_header_value_count("Content-Length")!=1) throw Error(411,"torrent requires one Content-Length");
    if (request.has_header("Content-Encoding")) throw Error(415,"compressed torrent unsupported");
    auto length=request.get_header_value_u64("Content-Length"); if (length<1 || length>1024*1024) throw Error(413,"torrent metadata exceeds 1 MiB");
}
void Bt::routes(httplib::Server& server) {
    streaming_routes(server);
    server.Post("/api/torrents",[this](const httplib::Request& request,httplib::Response& response) {
        preflight(request); auto info=validate(request.body); auto who=auth_.require(request,false,true);
        std::lock_guard lock(mutex_); Transaction tx(db_); who=auth_.require(request,false,true);
        if (db_.query("SELECT count(*) AS n FROM tasks")[0]["n"].get<int>()>=32 || db_.query("SELECT count(*) AS n FROM tasks WHERE owner=?",{who.id})[0]["n"].get<int>()>=8) throw Error(429,"BT task quota exceeded");
        auto shared=root(); (void)directory(shared);
        auto hash=info->info_hashes().v1; std::string digest(reinterpret_cast<const char*>(hash.data()),20);
        auto id=db_.insert("INSERT INTO tasks(owner,root,name,info_hash,created) VALUES(?,?,?,?,?)",{who.id,shared,info->name(),hex(digest),now()});
        auto path=config_.data/"bt-meta"/(std::to_string(id)+".torrent"); auto dir=directory(path.parent_path());
        int fd=::openat(dir->fd,path.filename().c_str(),O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW|O_CLOEXEC,0600); if (fd<0) throw Error(507,"cannot persist torrent metadata"); auto file=std::make_shared<File>(fd);
        try {
            if (::write(fd,request.body.data(),request.body.size())!=static_cast<ssize_t>(request.body.size()) || ::fsync(fd)!=0 || ::fsync(dir->fd)!=0) throw Error(507,"cannot sync torrent metadata");
            auto row=db_.query("SELECT * FROM tasks WHERE id=?",{id})[0]; add(row,info); audit("bt_imported",who.id,id); tx.commit(); reply(response,{{"task",status(row)}},201);
        } catch (...) { if (handles_.contains(id)) { session_->remove_torrent(handles_[id]); handles_.erase(id); } ::unlinkat(dir->fd,path.filename().c_str(),0); throw; }
    });
    server.Get("/api/tasks",[this](const httplib::Request& request,httplib::Response& response) {
        auto who=auth_.require(request); std::lock_guard lock(mutex_); drain(); Json tasks=Json::array();
        for (const auto& row:db_.query("SELECT * FROM tasks WHERE ?='admin' OR owner=? ORDER BY id DESC",{who.role,who.id})) tasks.push_back(status(row));
        reply(response,{{"tasks",tasks}});
    });
    const std::string task="/api/tasks/([1-9][0-9]{0,17})";
    server.Get(task,[this](const httplib::Request& request,httplib::Response& response) { std::lock_guard lock(mutex_); drain(); reply(response,{{"task",status(owned(request,false))}}); });
    server.Post(task+"/(pause|resume|recheck)",[this](const httplib::Request& request,httplib::Response& response) {
        std::lock_guard lock(mutex_); auto row=owned(request,true); auto id=row["id"].get<long long>();
        if (!handles_.contains(id)) throw Error(409,"task could not be restored");
        auto operation=request.matches[2].str();
        if (operation=="resume" && db_.query("SELECT disabled FROM users WHERE id=?",{row["owner"].get<long long>()})[0]["disabled"]==1) throw Error(409,"task owner is disabled");
        auto handle=handles_.at(id); auto who=auth_.require(request,false,true);
        Transaction tx(db_); (void)owned(request,true);
        if (operation=="pause") { handle.pause(); db_.query("UPDATE tasks SET paused=1 WHERE id=?",{id}); }
        else if (operation=="resume") { handle.clear_error(); faults_.erase(id); handle.resume(); handle.force_reannounce(0,-1,lt::torrent_handle::ignore_min_interval); db_.query("UPDATE tasks SET paused=0 WHERE id=?",{id}); }
        else {
            // Recreate this handle to discard old seed/peer state as well as resume bitmap.
            // Files remain in the same private directory and the fresh handle rehashes them.
            auto info=validate(read_metadata(config_.data/"bt-meta"/(std::to_string(id)+".torrent")));
            forget_stream(id); session_->remove_torrent(handle); handles_.erase(id); saving_.erase(id);
            db_.query("UPDATE tasks SET resume='' WHERE id=?",{id}); row["resume"]="";
            add(row,info); faults_.erase(id);
        }
        audit("bt_"+operation,who.id,id); tx.commit(); save(id); row["paused"]=operation=="pause"?1:operation=="resume"?0:row["paused"].get<int>(); reply(response,{{"task",status(row)}});
    });
    server.Delete(task,[this](const httplib::Request& request,httplib::Response& response) {
        std::lock_guard lock(mutex_); Transaction tx(db_); auto row=owned(request,true); auto id=row["id"].get<long long>(); auto who=auth_.require(request,false,true);
        forget_stream(id);
        if (handles_.contains(id)) { session_->remove_torrent(handles_.at(id)); handles_.erase(id); }
        saving_.erase(id); faults_.erase(id); db_.query("DELETE FROM tasks WHERE id=?",{id}); audit("bt_removed",who.id,id); tx.commit();
        // User data is deliberately retained in its private task directory.
        std::error_code error; fs::remove(config_.data/"bt-meta"/(std::to_string(id)+".torrent"),error); reply(response,{{"removed",true},{"downloaded_files_retained",true}});
    });
    server.Get("/api/bt/config",[this](const httplib::Request& request,httplib::Response& response) {
        auth_.require(request,true); std::lock_guard lock(mutex_); reply(response,{{"download_root",root()},{"proxy",proxy_summary(proxy_)},{"transport","TCP peers and HTTP(S) trackers; DHT, UDP, Web Seed disabled"}});
    });
    server.Post("/api/bt/root",[this](const httplib::Request& request,httplib::Response& response) {
        auto who=auth_.require(request,true,true); auto input=body(request); if (input.size()!=1 || !input.contains("path")) throw Error(400,"path required"); auto path=input["path"].get<std::string>();
        if (path.size()>4096 || path.find('\0')!=std::string::npos || !fs::path(path).is_absolute() || path=="/") throw Error(400,"absolute download directory required");
        auto dir=directory(path); (void)dir;
        std::lock_guard lock(mutex_); Transaction tx(db_); auth_.require(request,true,true); db_.query("UPDATE settings SET value=? WHERE key='download_root'",{path}); audit("bt_root_changed",who.id); tx.commit(); reply(response,{{"download_root",path},{"scope","new tasks only"}});
    });
    server.Post("/api/proxy",[this](const httplib::Request& request,httplib::Response& response) {
        auto who=auth_.require(request,true,true); if (request.body.size()>4096) throw Error(413,"proxy JSON too large"); auto input=validate_proxy(body(request));
        std::lock_guard lock(mutex_); for (const auto& row:db_.query("SELECT paused FROM tasks")) if (row["paused"]==0) throw Error(409,"pause all tasks before changing outbound proxy");
        for (const auto& [id,handle]:handles_) {
            (void)id; auto state=handle.status();
            if (!(state.flags&lt::torrent_flags::paused) || state.num_peers>0) throw Error(409,"wait for all peer connections to close before changing proxy");
        }
        Transaction tx(db_); auth_.require(request,true,true); db_.query("INSERT INTO settings(key,value) VALUES('bt_proxy',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",{encrypt(input)}); audit("bt_proxy_changed",who.id); tx.commit();
        proxy_=input; session_->apply_settings(settings(proxy_)); reply(response,{{"proxy",proxy_summary(proxy_)}});
    });
    server.Post("/api/proxy/test",[this](const httplib::Request& request,httplib::Response& response) {
        auth_.require(request,true,true); Json proxy;
        { std::lock_guard lock(mutex_); proxy=proxy_; }
        if (request.body.size()>4096) throw Error(413,"proxy test JSON too large");
        auto result=test_proxy(proxy,body(request)); auth_.require(request,true,true); reply(response,result);
    });
}
}
