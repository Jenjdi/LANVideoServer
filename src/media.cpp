#include "media.hpp"
#include <sodium.h>
#include <algorithm>
#include <array>
#include <charconv>
#include <chrono>
#include <cmath>
#include <fcntl.h>
#include <fstream>
#include <limits>
#include <set>
#include <sys/syscall.h>
#include <unistd.h>
namespace lv {
namespace {
// Managed BT files cannot enter ordinary media endpoints, including through an ancestor root.
// BT playback uses the separate verified-piece endpoint.
void catalog_only(fs::path path) {
    path=path.lexically_normal();
    if (path!=path.root_path() && path.filename().empty()) path=path.parent_path();
    for (;!path.empty();path=path.parent_path()) {
        auto folder=directory(path); struct stat marker{};
        if (::fstatat(folder->fd,".bt-task",&marker,AT_SYMLINK_NOFOLLOW)==0) throw Error(409,"managed BT directory cannot be shared through the ordinary media API");
        if (path==path.root_path()) break;
    }
}
Json json_input(const httplib::Request& request,std::initializer_list<const char*> allowed) {
    if (request.body.size()>4096) throw Error(413,"JSON request too large");
    auto content_type=request.get_header_value("Content-Type");
    if (content_type!="application/json" && !content_type.starts_with("application/json;")) throw Error(415,"application/json required");
    auto input=body(request);
    for (auto it=input.begin();it!=input.end();++it)
        if (std::none_of(allowed.begin(),allowed.end(),[&](const char* key){return it.key()==key;})) throw Error(400,"unknown input field");
    return input;
}
long long positive(const std::string& text) {
    long long value=0;
    auto result=std::from_chars(text.data(),text.data()+text.size(),value);
    if (result.ec!=std::errc{} || result.ptr!=text.data()+text.size() || value<1) throw Error(400,"invalid positive identifier");
    return value;
}
long long media_id(const httplib::Request& request) { return positive(request.matches[1].str()); }
Json public_media(const Json& row) {
    Json result={{"id",row["id"]},{"name",row["name"]},{"size",row["size"]},{"format",row["format"]},{"duration",row["duration"]},{"metadata_state",row["metadata_state"]}};
    result["position"]=row.value("position",Json(0.0));
    return result;
}
std::string query(const httplib::Request& request,const char* key,const std::string& fallback="") {
    if (request.get_param_value_count(key)>1) throw Error(400,"duplicate query parameter");
    return request.has_param(key) ? request.get_param_value(key) : fallback;
}
void query_fields(const httplib::Request& request,std::initializer_list<const char*> allowed) {
    for (const auto& item:request.params)
        if (std::none_of(allowed.begin(),allowed.end(),[&](const char* key){return item.first==key;})) throw Error(400,"unknown query parameter");
}
std::string mime(const std::string& format) {
    if (format=="mp4" || format=="m4v" || format=="mov") return "video/mp4";
    if (format=="webm") return "video/webm";
    if (format=="ogg" || format=="ogv") return "video/ogg";
    if (format=="mkv") return "video/x-matroska";
    if (format=="avi") return "video/x-msvideo";
    if (format=="ts") return "video/mp2t";
    if (format=="wmv") return "video/x-ms-wmv";
    return "video/mpeg";
}
bool same_file(const struct stat& stat,const Json& row) {
    return static_cast<long long>(stat.st_dev)==row["device"] && static_cast<long long>(stat.st_ino)==row["inode"] && stat.st_size==row["size"] && modified_ns(stat)==row["modified"];
}
std::string http_date(time_t timestamp) {
    tm value{}; gmtime_r(&timestamp,&value); std::array<char,80> buffer{};
    strftime(buffer.data(),buffer.size(),"%a, %d %b %Y %H:%M:%S GMT",&value);
    return buffer.data();
}
// cpp-httplib constructs a mutable Request then exposes it as const to routes.
// Its automatic Range pass would otherwise slice our selected response twice.
// Keep this pinned-version transport adaptation isolated from application logic.
void disable_library_ranges(const httplib::Request& request) {
    const_cast<httplib::Request&>(request).ranges.clear();
}
struct Slice { size_t begin=0,length=0; bool partial=false; };
Slice range(const httplib::Request& request,size_t size,const std::string& last_modified) {
    Slice result{0,size,false};
    if (!request.has_header("Range") || request.method=="HEAD") return result;
    if (request.get_header_value_count("Range")!=1) throw Error(416,"one Range header required");
    if (request.has_header("If-Range") && request.get_header_value("If-Range")!=last_modified) return result;
    auto text=request.get_header_value("Range");
    if (!text.starts_with("bytes=") || text.find(',')!=std::string::npos || size==0) throw Error(416,"only one satisfiable byte range is supported");
    text.erase(0,6); auto dash=text.find('-');
    if (dash==std::string::npos || text.find('-',dash+1)!=std::string::npos) throw Error(416,"invalid byte range");
    auto parse=[](const std::string& value) {
        size_t number=0; auto converted=std::from_chars(value.data(),value.data()+value.size(),number);
        if (value.empty() || converted.ec!=std::errc{} || converted.ptr!=value.data()+value.size()) throw Error(416,"invalid range number");
        return number;
    };
    auto left=text.substr(0,dash),right=text.substr(dash+1);
    if (left.empty()) {
        auto suffix=parse(right); if (suffix==0) throw Error(416,"zero suffix range");
        result.length=std::min(suffix,size); result.begin=size-result.length;
    } else {
        result.begin=parse(left); if (result.begin>=size) throw Error(416,"range starts beyond the file");
        auto end=right.empty() ? size-1 : parse(right);
        if (end<result.begin) throw Error(416,"range end precedes start");
        end=std::min(end,size-1); result.length=end-result.begin+1;
    }
    result.partial=true; return result;
}
struct TemporaryUpload {
    FilePtr folder,file;
    std::string temporary,published;
    bool kept=false;
    ~TemporaryUpload() {
        if (!temporary.empty()) ::unlinkat(folder->fd,temporary.c_str(),0);
        if (!kept && !published.empty()) {
            struct stat target{};
            if (::fstatat(folder->fd,published.c_str(),&target,AT_SYMLINK_NOFOLLOW)==0) {
                struct stat mine{};
                if (file && ::fstat(file->fd,&mine)==0 && target.st_dev==mine.st_dev && target.st_ino==mine.st_ino) ::unlinkat(folder->fd,published.c_str(),0);
            }
        }
    }
};
}
Media::Media(Database& database,Auth& auth,const Config& config):database_(database),auth_(auth),config_(config) {
    Transaction transaction(database_);
    database_.query("CREATE TABLE IF NOT EXISTS media(id INTEGER PRIMARY KEY AUTOINCREMENT,root TEXT NOT NULL,relative TEXT NOT NULL,name TEXT NOT NULL,size INTEGER NOT NULL,format TEXT NOT NULL,duration REAL,metadata_state TEXT NOT NULL,device INTEGER NOT NULL,inode INTEGER NOT NULL,modified INTEGER NOT NULL,UNIQUE(root,relative))");
    database_.query("CREATE TABLE IF NOT EXISTS grants(media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,PRIMARY KEY(media_id,user_id))");
    database_.query("CREATE TABLE IF NOT EXISTS history(user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,position REAL NOT NULL CHECK(position>=0),updated INTEGER NOT NULL,PRIMARY KEY(user_id,media_id))");
    if (database_.query("SELECT value FROM settings WHERE key='media_root'").empty()) {
        auto shared=config.data/"shared";
        if (fs::is_symlink(fs::symlink_status(shared))) throw Error(403,"default shared directory must not be a symlink");
        fs::create_directories(shared);
        auto canonical=fs::canonical(shared); (void)directory(canonical);
        database_.query("INSERT INTO settings(key,value) VALUES('media_root',?)",{canonical.string()});
    }
    transaction.commit();
}
std::string Media::root() { return database_.query("SELECT value FROM settings WHERE key='media_root'")[0]["value"].get<std::string>(); }
Json Media::lookup(long long id,const Identity& who) {
    auto rows=database_.query("SELECT m.*,coalesce(h.position,0.0) AS position FROM media m LEFT JOIN history h ON h.media_id=m.id AND h.user_id=? WHERE m.id=? AND m.root=?",{who.id,id,root()});
    if (rows.empty()) throw Error(404,"media not found");
    if (who.role!="admin" && database_.query("SELECT 1 FROM grants WHERE user_id=? AND media_id=?",{who.id,id}).empty()) throw Error(403,"media access not granted");
    return rows[0];
}
void Media::finish(Transaction& transaction,const std::string& event,long long actor,const Json& detail) {
    database_.query("INSERT INTO audit(timestamp,actor,event,detail) VALUES(?,?,?,?)",{now(),actor,event,detail.dump()});
    transaction.commit(); log(event,{{"actor",actor},{"detail",detail}});
}
Json Media::register_file(const std::string& shared,const std::string& relative,const FilePtr& file,const Probe& probe) {
    catalog_only(fs::path(shared)/fs::path(relative).parent_path());
    auto stat=file->stat();
    auto id=database_.insert("INSERT INTO media(root,relative,name,size,format,duration,metadata_state,device,inode,modified) VALUES(?,?,?,?,?,?,?,?,?,?)",{shared,relative,fs::path(relative).filename().string(),static_cast<long long>(stat.st_size),format_of(relative),probe.duration,probe.state,static_cast<long long>(stat.st_dev),static_cast<long long>(stat.st_ino),modified_ns(stat)});
    return database_.query("SELECT * FROM media WHERE id=?",{id})[0];
}
void Media::serve(const httplib::Request& request,httplib::Response& response,bool invalid_range) {
    serve_authorized(request,response,[this,request] {
        auto who=auth_.require(request);
        return lookup(positive(request.path.substr(7)),who);
    },invalid_range);
}
void Media::serve_authorized(const httplib::Request& request,httplib::Response& response,std::function<Json()> authorize,bool invalid_range) {
    Json row; FilePtr file;
    {
        std::lock_guard lock(database_.mutex());
        row=authorize();
        catalog_only(fs::path(row["root"].get<std::string>())/fs::path(row["relative"].get<std::string>()).parent_path());
        file=regular_file(directory(row["root"].get<std::string>()),row["relative"].get<std::string>());
        if (!same_file(file->stat(),row)) throw Error(409,"media changed; administrator must rescan and reauthorize");
    }
    auto stat=file->stat();
    auto size=static_cast<size_t>(stat.st_size);
    response.set_header("Accept-Ranges","bytes");
    auto last_modified=http_date(stat.st_mtim.tv_sec);
    response.set_header("Last-Modified",last_modified);
    response.set_header("Content-Disposition","inline; filename*=UTF-8''"+encoded_filename(row["name"].get<std::string>()));
    Slice selected;
    try {
        if (invalid_range && request.method!="HEAD") throw Error(416,"invalid Range syntax");
        selected=range(request,size,last_modified);
    } catch (const Error& error) {
        disable_library_ranges(request);
        response.set_header("Content-Range","bytes */"+std::to_string(size));
        reply(response,{{"error",error.what()}},416); return;
    }
    disable_library_ranges(request);
    response.status=selected.partial ? 206 : 200;
    if (selected.partial) response.set_header("Content-Range","bytes "+std::to_string(selected.begin)+"-"+std::to_string(selected.begin+selected.length-1)+"/"+std::to_string(size));
    if (selected.length==0) { response.set_content("",mime(row["format"].get<std::string>())); return; }
    response.set_content_provider(selected.length,mime(row["format"].get<std::string>()),[this,file,selected,row,authorize](size_t offset,size_t length,httplib::DataSink& sink) {
        if (offset>selected.length || length>selected.length-offset) return false;
        try {
            std::lock_guard lock(database_.mutex());
            auto current=authorize();
            if (current["id"]!=row["id"] || current["root"]!=row["root"] || current["relative"]!=row["relative"] || current["root"]!=root()) return false;
            catalog_only(fs::path(current["root"].get<std::string>())/fs::path(current["relative"].get<std::string>()).parent_path());
            auto path_file=regular_file(directory(current["root"].get<std::string>()),current["relative"].get<std::string>());
            if (!same_file(path_file->stat(),row) || !same_file(file->stat(),row)) return false;
        } catch (...) { return false; }
        std::array<char,64*1024> buffer{};
        size_t count=std::min(length,buffer.size());
        auto n=::pread(file->fd,buffer.data(),count,static_cast<off_t>(selected.begin+offset));
        if (n<=0) return false;
        return sink.write(buffer.data(),static_cast<size_t>(n));
    });
}
void Media::range_failure(const httplib::Request& request,httplib::Response& response) {
    if (!request.path.starts_with("/media/")) return;
    try { serve(request,response,true); }
    catch (const Error& error) { disable_library_ranges(request); reply(response,{{"error",error.what()}},error.status); }
    catch (...) { disable_library_ranges(request); reply(response,{{"error","media request failed"}},500); }
}
void Media::upload_preflight(const httplib::Request& request) {
    auth_.require(request,true,true);
    query_fields(request,{"filename"}); upload_name(query(request,"filename"));
    if (request.get_header_value("Content-Type")!="application/octet-stream") throw Error(415,"upload raw file with application/octet-stream");
    if (request.has_header("Transfer-Encoding") || request.get_header_value_count("Content-Length")!=1) throw Error(411,"upload requires one Content-Length; chunked upload unsupported");
    if (request.has_header("Content-Encoding") && request.get_header_value("Content-Encoding")!="identity") throw Error(415,"compressed uploads unsupported");
    auto value=request.get_header_value("Content-Length"); size_t length=0;
    auto parsed=std::from_chars(value.data(),value.data()+value.size(),length);
    if (parsed.ec!=std::errc{} || parsed.ptr!=value.data()+value.size() || length==0) throw Error(400,"invalid or empty upload length");
    if (length>config_.upload_limit) throw Error(413,"video exceeds configured upload limit");
}
void Media::upload(const httplib::Request& request,httplib::Response& response,const httplib::ContentReader& reader) {
    upload_preflight(request);
    std::unique_lock operation(operations_,std::try_to_lock);
    if (!operation.owns_lock()) throw Error(409,"another scan, root change or upload is active");
    auto shared=root(),name=query(request,"filename");
    auto folder=directory(shared);
    std::array<unsigned char,16> random{}; randombytes_buf(random.data(),random.size());
    std::array<char,33> hex{}; sodium_bin2hex(hex.data(),hex.size(),random.data(),random.size());
    TemporaryUpload pending{folder,nullptr,".upload-"+std::string(hex.data()),"",false};
    int fd=::openat(folder->fd,pending.temporary.c_str(),O_CREAT|O_EXCL|O_RDWR|O_NOFOLLOW|O_CLOEXEC,0600);
    if (fd<0) { pending.temporary.clear(); throw Error(507,"cannot create upload staging file"); }
    pending.file=std::make_shared<File>(fd);
    size_t received=0,expected=static_cast<size_t>(request.get_header_value_u64("Content-Length"));
    auto started=std::chrono::steady_clock::now();
    int failure=400;
    bool complete=reader([&](const char* data,size_t length) {
        if (std::chrono::steady_clock::now()-started>std::chrono::seconds(120)) { failure=408; return false; }
        if (length>config_.upload_limit-received || length>expected-received) { failure=413; return false; }
        size_t done=0;
        while (done<length) {
            auto n=::write(fd,data+done,length-done);
            if (n<0 && errno==EINTR) continue;
            if (n<=0) { failure=507; return false; }
            done+=static_cast<size_t>(n);
        }
        received+=length; return true;
    });
    if (!complete || received!=expected) throw Error(failure,"upload interrupted, timed out or exceeded limit; no file published");
    if (::fsync(fd)!=0) throw Error(507,"cannot sync upload data");
    if (!container_signature(pending.file,format_of(name))) throw Error(415,"file does not match a supported video container");
    auto metadata=probe_video(pending.file,config_.ffprobe);
    if (metadata.state=="invalid") throw Error(415,"FFprobe found no readable video stream");
    if (request.is_connection_closed && request.is_connection_closed()) throw Error(400,"upload connection closed before publication");
    Transaction transaction(database_); auto who=auth_.require(request,true,true);
    if (root()!=shared) throw Error(409,"shared root changed during upload");
    auto current_folder=directory(shared); auto old_stat=folder->stat(),current_stat=current_folder->stat();
    if (old_stat.st_dev!=current_stat.st_dev || old_stat.st_ino!=current_stat.st_ino) throw Error(409,"shared directory changed during upload");
    if (request.is_connection_closed && request.is_connection_closed()) throw Error(400,"upload connection closed before publication");
    if (::syscall(SYS_renameat2,folder->fd,pending.temporary.c_str(),folder->fd,name.c_str(),RENAME_NOREPLACE)!=0)
        throw Error(errno==EEXIST ? 409 : 507,"upload destination exists or cannot be published");
    pending.published=name; pending.temporary.clear();
    // A missing old file may still have stale catalog permissions: never inherit them.
    database_.query("DELETE FROM media WHERE root=? AND relative=?",{shared,name});
    auto item=register_file(shared,name,pending.file,metadata);
    if (::fsync(folder->fd)!=0) throw Error(507,"cannot sync upload publication");
    finish(transaction,"media_uploaded",who.id,{{"media_id",item["id"]},{"bytes",received}});
    pending.kept=true; reply(response,{{"media",public_media(item)}},201);
}
void Media::routes(httplib::Server& server) {
    server.Get("/api/media/config",[this](const httplib::Request& request,httplib::Response& response) {
        std::lock_guard lock(database_.mutex()); auth_.require(request,true);
        reply(response,{{"root",root()},{"upload_limit_bytes",config_.upload_limit},{"ffprobe_available",!config_.ffprobe.empty() && ::access(config_.ffprobe.c_str(),X_OK)==0}});
    });
    server.Post("/api/media/root",[this](const httplib::Request& request,httplib::Response& response) {
        auth_.require(request,true,true); auto input=json_input(request,{"path"});
        if (!input.contains("path") || !input["path"].is_string()) throw Error(400,"path string required");
        auto text=input["path"].get<std::string>();
        if (text.empty() || text.size()>4096 || text.find('\0')!=std::string::npos || !fs::path(text).is_absolute()) throw Error(400,"root must be an absolute directory path");
        std::error_code error; auto canonical=fs::canonical(text,error);
        if (error) throw Error(404,"shared directory does not exist");
        if (canonical=="/") throw Error(400,"cannot share the entire filesystem");
        (void)directory(canonical); catalog_only(canonical);
        std::unique_lock operation(operations_,std::try_to_lock); if (!operation.owns_lock()) throw Error(409,"media operation busy");
        Transaction transaction(database_); auto who=auth_.require(request,true,true);
        bool changed=root()!=canonical.string();
        if (changed) { database_.query("DELETE FROM media"); database_.query("UPDATE settings SET value=? WHERE key='media_root'",{canonical.string()}); }
        finish(transaction,"media_root_changed",who.id,{{"changed",changed}});
        reply(response,{{"root",canonical.string()},{"catalog_reset",changed}});
    });
    server.Post("/api/media/scan",[this](const httplib::Request& request,httplib::Response& response) {
        auth_.require(request,true,true);
        if (!request.body.empty()) { auto input=json_input(request,{}); (void)input; }
        std::unique_lock operation(operations_,std::try_to_lock); if (!operation.owns_lock()) throw Error(409,"media operation busy");
        auto shared=root(); auto folder=directory(shared); catalog_only(shared);
        struct Candidate { std::string relative; FilePtr file; Probe metadata; };
        std::vector<Candidate> candidates; size_t entries=0,skipped=0;
        auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(20);
        std::error_code error;
        fs::recursive_directory_iterator it(shared,error),end;
        if (error) throw Error(403,"cannot scan shared directory");
        for (;it!=end;it.increment(error)) {
            if (error) throw Error(403,"directory scan failed; no catalog changes applied");
            if (++entries>10000 || std::chrono::steady_clock::now()>deadline) throw Error(422,"scan exceeds 10000 entries or 20 seconds; select a smaller root");
            auto filename=it->path().filename().string();
            auto status=it->symlink_status(error); if (error) throw Error(403,"cannot inspect scan entry");
            if (fs::is_symlink(status) || filename.starts_with('.') || it.depth()>31) { it.disable_recursion_pending(); ++skipped; continue; }
            if (fs::is_directory(status)) {
                auto dir=directory(it->path()); struct stat marker{};
                if (::fstatat(dir->fd,".bt-task",&marker,AT_SYMLINK_NOFOLLOW)==0) { it.disable_recursion_pending(); ++skipped; }
                continue;
            }
            if (!fs::is_regular_file(status) || !video_format(format_of(filename))) continue;
            if (candidates.size()>=500) throw Error(422,"scan supports at most 500 video files per root");
            try {
                (void)Json(filename).dump();
                auto relative=it->path().lexically_relative(shared).string();
                catalog_only(it->path().parent_path());
                auto file=regular_file(folder,relative); auto before=file->stat();
                auto metadata=probe_video(file,config_.ffprobe); auto after=file->stat();
                if (before.st_size!=after.st_size || modified_ns(before)!=modified_ns(after)) { ++skipped; continue; }
                candidates.push_back({relative,file,metadata});
            } catch (const Error&) { ++skipped; }
            catch (const Json::exception&) { ++skipped; }
        }
        if (error) throw Error(403,"directory scan failed; no catalog changes applied");
        Transaction transaction(database_); auto who=auth_.require(request,true,true);
        if (root()!=shared) throw Error(409,"root changed during scan");
        std::set<std::string> seen; size_t changed=0,removed=0,unavailable=0;
        for (const auto& candidate:candidates) {
            auto rows=database_.query("SELECT * FROM media WHERE root=? AND relative=?",{shared,candidate.relative});
            if (!rows.empty() && same_file(candidate.file->stat(),rows[0]))
                database_.query("UPDATE media SET duration=?,metadata_state=? WHERE id=?",{candidate.metadata.duration,candidate.metadata.state,rows[0]["id"]});
            else {
                if (!rows.empty()) database_.query("DELETE FROM media WHERE id=?",{rows[0]["id"].get<long long>()});
                (void)register_file(shared,candidate.relative,candidate.file,candidate.metadata); ++changed;
            }
            seen.insert(candidate.relative); if (candidate.metadata.state!="ok") ++unavailable;
        }
        for (const auto& row:database_.query("SELECT id,relative FROM media WHERE root=?",{shared})) {
            if (!seen.contains(row["relative"].get<std::string>())) { database_.query("DELETE FROM media WHERE id=?",{row["id"].get<long long>()}); ++removed; }
        }
        finish(transaction,"media_scanned",who.id,{{"indexed",candidates.size()},{"changed",changed},{"removed",removed},{"skipped",skipped}});
        reply(response,{{"indexed",candidates.size()},{"changed",changed},{"removed",removed},{"skipped",skipped},{"metadata_unavailable",unavailable}});
    });
    server.Get("/api/media",[this](const httplib::Request& request,httplib::Response& response) {
        query_fields(request,{"q","format","limit","offset"});
        auto search=query(request,"q"),format=query(request,"format");
        if (search.size()>200 || search.find('\0')!=std::string::npos || (!format.empty() && !video_format(format))) throw Error(400,"invalid search or format");
        auto limit=positive(query(request,"limit","100")); if (limit>200) throw Error(400,"limit must be 1 to 200");
        auto offset_text=query(request,"offset","0"); long long offset=offset_text=="0" ? 0 : positive(offset_text);
        if (offset>1000000) throw Error(400,"offset too large");
        std::lock_guard lock(database_.mutex()); auto who=auth_.require(request);
        auto predicate=std::string("m.root=? AND instr(lower(m.name),lower(?))>0 AND (?='' OR m.format=?) AND (?='admin' OR EXISTS(SELECT 1 FROM grants g WHERE g.media_id=m.id AND g.user_id=?))");
        auto shared=root(); Json params={shared,search,format,format,who.role,who.id};
        auto total=database_.query("SELECT count(*) AS total FROM media m WHERE "+predicate,params)[0]["total"];
        Json values={who.id}; for (const auto& value:params) values.push_back(value); values.push_back(limit); values.push_back(offset);
        auto rows=database_.query("SELECT m.*,coalesce(h.position,0.0) AS position FROM media m LEFT JOIN history h ON h.media_id=m.id AND h.user_id=? WHERE "+predicate+" ORDER BY m.name,m.id LIMIT ? OFFSET ?",values);
        Json result=Json::array(); for (const auto& row:rows) result.push_back(public_media(row));
        reply(response,{{"media",result},{"total",total},{"limit",limit},{"offset",offset}});
    });
    server.Get(R"(/api/media/(\d+)/grants)",[this](const httplib::Request& request,httplib::Response& response) {
        std::lock_guard lock(database_.mutex()); auto who=auth_.require(request,true); auto id=media_id(request); (void)lookup(id,who);
        Json ids=Json::array(); for (const auto& row:database_.query("SELECT user_id FROM grants WHERE media_id=? ORDER BY user_id",{id})) ids.push_back(row["user_id"]);
        reply(response,{{"user_ids",ids}});
    });
    server.Post(R"(/api/media/(\d+)/grants)",[this](const httplib::Request& request,httplib::Response& response) {
        Transaction transaction(database_); auto who=auth_.require(request,true,true); auto id=media_id(request); (void)lookup(id,who);
        auto input=json_input(request,{"user_ids"});
        if (!input.contains("user_ids") || !input["user_ids"].is_array() || input["user_ids"].size()>500) throw Error(400,"user_ids must be an array of at most 500 IDs");
        std::set<long long> ids;
        for (const auto& value:input["user_ids"]) {
            if (!value.is_number_integer() || (value.is_number_unsigned() && value.get<unsigned long long>()>static_cast<unsigned long long>(std::numeric_limits<long long>::max()))) throw Error(400,"invalid user id type");
            auto user_id=value.get<long long>(); if (user_id<1 || !ids.insert(user_id).second) throw Error(400,"IDs must be positive and unique");
            if (database_.query("SELECT id FROM users WHERE id=? AND disabled=0 AND role='user'",{user_id}).empty()) throw Error(400,"grant target must be an enabled regular user");
        }
        database_.query("DELETE FROM grants WHERE media_id=?",{id});
        for (auto user_id:ids) database_.query("INSERT INTO grants(media_id,user_id) VALUES(?,?)",{id,user_id});
        database_.query("DELETE FROM history WHERE media_id=? AND user_id IN(SELECT id FROM users WHERE role='user') AND user_id NOT IN(SELECT user_id FROM grants WHERE media_id=?)",{id,id});
        finish(transaction,"media_grants_changed",who.id,{{"media_id",id},{"users",ids.size()}}); reply(response,{{"ok",true}});
    });
    server.Get(R"(/api/media/(\d+))",[this](const httplib::Request& request,httplib::Response& response) {
        std::lock_guard lock(database_.mutex()); auto who=auth_.require(request); reply(response,{{"media",public_media(lookup(media_id(request),who))}});
    });
    server.Get(R"(/media/(\d+))",[this](const httplib::Request& request,httplib::Response& response) { serve(request,response); });
    server.Post("/api/media/upload",[this](const httplib::Request& request,httplib::Response& response,const httplib::ContentReader& reader) { upload(request,response,reader); });
    server.Post("/api/history",[this](const httplib::Request& request,httplib::Response& response) {
        Transaction transaction(database_); auto who=auth_.require(request,false,true); auto input=json_input(request,{"media_id","position"});
        if (!input.contains("media_id") || !input["media_id"].is_number_integer() || (input["media_id"].is_number_unsigned() && input["media_id"].get<unsigned long long>()>static_cast<unsigned long long>(std::numeric_limits<long long>::max()))) throw Error(400,"media_id integer required");
        auto id=input["media_id"].get<long long>(); if (id<1) throw Error(400,"invalid media ID");
        auto item=lookup(id,who);
        if (!input.contains("position") || !input["position"].is_number()) throw Error(400,"numeric position required");
        double position=input["position"].get<double>();
        if (!std::isfinite(position) || position<0 || position>2592000 || (!item["duration"].is_null() && position>item["duration"].get<double>()+1)) throw Error(400,"position outside video duration or limit");
        database_.query("INSERT INTO history(user_id,media_id,position,updated) VALUES(?,?,?,?) ON CONFLICT(user_id,media_id) DO UPDATE SET position=excluded.position,updated=excluded.updated",{who.id,id,position,now()});
        transaction.commit(); reply(response,{{"ok",true}});
    });
    server.Get("/api/history",[this](const httplib::Request& request,httplib::Response& response) {
        query_fields(request,{}); std::lock_guard lock(database_.mutex()); auto who=auth_.require(request);
        auto rows=database_.query("SELECT h.media_id,h.position,h.updated,m.name FROM history h JOIN media m ON m.id=h.media_id WHERE h.user_id=? AND m.root=? AND (?='admin' OR EXISTS(SELECT 1 FROM grants g WHERE g.media_id=m.id AND g.user_id=?)) ORDER BY h.updated DESC,h.media_id LIMIT 100",{who.id,root(),who.role,who.id});
        reply(response,{{"history",rows}});
    });
    for (const auto& entry:std::vector<std::pair<std::string,std::string>>{{"/","index.html"},{"/app.js","app.js"},{"/style.css","style.css"},{"/sha256.min.js","sha256.min.js"}}) {
        auto folder=directory(config_.web); auto name=entry.second;
        auto type=entry.second=="index.html" ? "text/html; charset=utf-8" : entry.second.ends_with(".js") ? "text/javascript; charset=utf-8" : "text/css; charset=utf-8";
        server.Get(entry.first,[folder,name,type](const httplib::Request&,httplib::Response& response) {
            auto file=regular_file(folder,name); auto stat=file->stat();
            if (stat.st_size>2*1024*1024) throw Error(500,"web asset too large");
            std::string content(static_cast<size_t>(stat.st_size),'\0');
            if (::pread(file->fd,content.data(),content.size(),0)!=stat.st_size) throw Error(500,"web asset read failed");
            response.set_content(content,type); response.status=200;
        });
    }
    server.Get("/favicon.ico",[](const auto&,auto& response){response.status=204;});
}
}
