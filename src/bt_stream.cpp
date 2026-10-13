#include "bt.hpp"
#include <libtorrent/alert_types.hpp>
#include <libtorrent/peer_request.hpp>
#include <libtorrent/file_storage.hpp>
#include <libtorrent/download_priority.hpp>
#include <openssl/evp.h>
#include <sodium.h>
#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <fcntl.h>
#include <limits>
#include <regex>
#include <unistd.h>
namespace lv {
namespace lt=libtorrent;
namespace {
using Clock=std::chrono::steady_clock;
constexpr size_t window_bytes=1024*1024;
int file_index(const httplib::Request& request,const lt::torrent_info& info) {
    auto text=request.matches[2].str(); int index=-1;
    auto parsed=std::from_chars(text.data(),text.data()+text.size(),index);
    if (parsed.ec!=std::errc{} || parsed.ptr!=text.data()+text.size() || index<0 || index>=info.files().num_files()) throw Error(404,"torrent file index not found");
    if (!video_format(format_of(info.files().file_path(lt::file_index_t(index))))) throw Error(415,"this torrent file is not a supported video format");
    return index;
}
bool checked(const lt::torrent_status& state) {
    return state.state==lt::torrent_status::downloading || state.state==lt::torrent_status::finished || state.state==lt::torrent_status::seeding;
}
bool present(const lt::torrent_status& state,int piece) {
    return checked(state) && piece>=0 && piece<state.pieces.size() && state.pieces[lt::piece_index_t(piece)];
}
void clear_ranges(const httplib::Request& request) { const_cast<httplib::Request&>(request).ranges.clear(); }
size_t unsigned_number(const std::string& text) {
    size_t result=0; auto parsed=std::from_chars(text.data(),text.data()+text.size(),result);
    if (text.empty() || parsed.ec!=std::errc{} || parsed.ptr!=text.data()+text.size()) throw Error(416,"invalid byte range");
    return result;
}
struct Slice { size_t begin=0,length=0; bool partial=false,open=false; };
Slice slice(const httplib::Request& request,size_t size,bool invalid) {
    Slice result{0,size,false,false};
    if (request.method=="HEAD" || (!invalid && !request.has_header("Range"))) return result;
    if (invalid || request.get_header_value_count("Range")!=1 || size==0) throw Error(416,"one satisfiable byte range required");
    auto range=request.get_header_value("Range");
    if (!range.starts_with("bytes=") || range.find(',')!=std::string::npos) throw Error(416,"one byte range required");
    range.erase(0,6); auto dash=range.find('-');
    if (dash==std::string::npos || range.find('-',dash+1)!=std::string::npos) throw Error(416,"invalid byte range");
    auto left=range.substr(0,dash),right=range.substr(dash+1);
    if (left.empty()) { auto suffix=unsigned_number(right); if (!suffix) throw Error(416,"zero suffix range"); result.length=std::min(suffix,size); result.begin=size-result.length; }
    else {
        result.begin=unsigned_number(left); if (result.begin>=size) throw Error(416,"range begins beyond video");
        auto end=right.empty()?size-1:unsigned_number(right); if (end<result.begin) throw Error(416,"range end precedes start");
        result.length=std::min(end,size-1)-result.begin+1; result.open=right.empty();
    }
    result.partial=true; return result;
}
std::string type(const std::string& name) {
    auto format=format_of(name);
    if (format=="mp4" || format=="m4v" || format=="mov") return "video/mp4";
    if (format=="webm") return "video/webm";
    if (format=="ogg" || format=="ogv") return "video/ogg";
    if (format=="mkv") return "video/x-matroska";
    if (format=="avi") return "video/x-msvideo";
    if (format=="wmv") return "video/x-ms-wmv";
    if (format=="ts") return "video/mp2t";
    return "video/mpeg";
}
}
void Bt::release_focus(const Focus& focus) {
    if (!focus.handle.is_valid()) return;
    if (!focus.pieces.empty()) focus.handle.reset_piece_deadline(lt::piece_index_t(focus.pieces.front()));
    for (int piece:focus.pieces) focus.handle.piece_priority(lt::piece_index_t(piece),lt::default_priority);
}
void Bt::forget_stream(long long id) {
    for (auto it=reads_.begin();it!=reads_.end();) {
        if (it->first.first==id) { it->second->ready=true; it->second->error=503; it=reads_.erase(it); } else ++it;
    }
    for (auto it=quarantined_.begin();it!=quarantined_.end();) if (it->first==id) it=quarantined_.erase(it); else ++it;
    if (focus_.contains(id)) release_focus(focus_.at(id));
    focus_.erase(id); directories_.erase(id);
}
void Bt::read_alert(const lt::alert* alert,long long id) {
    auto read=lt::alert_cast<lt::read_piece_alert>(alert); if (!read) return;
    auto key=std::pair(id,static_cast<int>(read->piece)); auto found=reads_.find(key);
    if (found==reads_.end() || found->second->handle!=read->handle) return;
    auto& job=*found->second; job.ready=true;
    auto info=read->handle.torrent_file();
    if (read->error || !read->buffer || !info || read->size!=info->piece_size(read->piece)) { job.error=503; return; }
    std::array<unsigned char,EVP_MAX_MD_SIZE> digest{}; unsigned int length=0;
    auto expected=info->hash_for_piece(read->piece);
    if (EVP_Digest(read->buffer.get(),static_cast<size_t>(read->size),digest.data(),&length,EVP_sha1(),nullptr)!=1 || length!=20) { job.error=503; return; }
    if (sodium_memcmp(digest.data(),expected.data(),20)!=0) {
        job.error=409; quarantined_.insert(key);
        log("bt_stream_checksum_failed",{{"task_id",id},{"piece",key.second}},"error"); return;
    }
    // Immutable snapshot: disk mutations after this hash cannot alter transmitted bytes.
    job.bytes=std::make_shared<const std::string>(read->buffer.get(),static_cast<size_t>(read->size));
}
void Bt::focus(long long id,const lt::torrent_handle& handle,int piece) {
    auto info=handle.torrent_file(); if (!info || piece<0 || piece>=info->num_pieces()) return;
    auto found=focus_.find(id);
    auto count=std::min(128,std::max(1,static_cast<int>((window_bytes+static_cast<size_t>(info->piece_length())-1)/static_cast<size_t>(info->piece_length()))));
    auto end=std::min(info->num_pieces(),piece+count);
    std::vector<int> wanted; for (int i=piece;i<end;++i) wanted.push_back(i);
    if (found!=focus_.end() && found->second.handle==handle && found->second.pieces==wanted) { found->second.expires=Clock::now()+std::chrono::seconds(10); schedule(); return; }
    if (found!=focus_.end()) release_focus(found->second);
    focus_[id]=Focus{handle,std::move(wanted),Clock::now()+std::chrono::seconds(10),false}; schedule();
}
void Bt::schedule() {
    // 2 seconds focused, 1 second with every piece at baseline: moving playback
    // windows do not permanently lock the library onto priority 7 pieces.
    auto now=Clock::now(); auto tick=std::chrono::duration_cast<std::chrono::milliseconds>(now.time_since_epoch()).count();
    bool high=(tick%3000)<2000;
    for (auto it=focus_.begin();it!=focus_.end();) {
        auto& f=it->second;
        if (!handles_.contains(it->first) || handles_.at(it->first)!=f.handle || f.expires<=now || stopping_) {
            release_focus(f);
            it=focus_.erase(it); continue;
        }
        if (f.elevated!=high) {
            if (!high) f.handle.reset_piece_deadline(lt::piece_index_t(f.pieces.front()));
            for (int i:f.pieces) f.handle.piece_priority(lt::piece_index_t(i),high?lt::top_priority:lt::default_priority);
            if (high) f.handle.set_piece_deadline(lt::piece_index_t(f.pieces.front()),500);
            f.elevated=high;
        }
        ++it;
    }
    for (auto it=reads_.begin();it!=reads_.end();) {
        if (now-it->second->created>std::chrono::seconds(5)) { it->second->ready=true; it->second->error=503; it=reads_.erase(it); } else ++it;
    }
}
Json Bt::schedule_status(long long id) {
    schedule(); Json result={{"focus",nullptr},{"window_bytes",window_bytes},{"focus_seconds",2},{"background_seconds",1},{"lease_seconds",10}};
    if (!focus_.contains(id)) return result;
    auto& f=focus_.at(id); Json pieces=Json::array();
    for (int i:f.pieces) pieces.push_back({{"index",i},{"priority",static_cast<int>(f.handle.piece_priority(lt::piece_index_t(i)))}});
    result["focus"]={{"pieces",pieces},{"phase",f.elevated?"focus":"background"}}; return result;
}
void Bt::validate_stream_path(const Json& row,const lt::torrent_info& info,int piece) {
    auto id=row["id"].get<long long>(); if (!directories_.contains(id)) throw Error(409,"task directory unavailable");
    auto path=fs::path(row["root"].get<std::string>())/("task-"+std::to_string(id)); auto folder=directory(path);
    auto original=directories_.at(id)->stat(),current=folder->stat();
    if (current.st_dev!=original.st_dev || current.st_ino!=original.st_ino || current.st_uid!=::geteuid() || (current.st_mode&077)!=0) throw Error(409,"task directory identity or permissions changed");
    for (const auto& part:info.files().map_block(lt::piece_index_t(piece),0,info.piece_size(lt::piece_index_t(piece)))) {
        auto file=regular_file(folder,info.files().file_path(part.file_index));
        if (file->stat().st_nlink!=1) throw Error(403,"torrent hard links cannot be streamed");
    }
}
std::shared_ptr<const std::string> Bt::read_verified(const httplib::Request& request,long long id,const lt::torrent_handle& handle,int piece,Clock::time_point deadline) {
    auto key=std::pair(id,piece); std::shared_ptr<PieceRead> job;
    while (Clock::now()<deadline) {
        if (stopping_) throw Error(503,"service stopping; retry after restart");
        if (request.is_connection_closed && request.is_connection_closed()) throw Error(400,"stream client disconnected");
        {
            std::lock_guard lock(mutex_); auto row=owned(request,false);
            if (!handles_.contains(id) || handles_.at(id)!=handle) throw Error(503,"task changed; retry stream");
            if (quarantined_.contains(key)) throw Error(409,"piece checksum failed; pause and recheck this task");
            auto state=handle.status(lt::torrent_handle::query_pieces); focus(id,handle,piece);
            if (present(state,piece) && handle.have_piece(lt::piece_index_t(piece))) {
                auto info=handle.torrent_file(); validate_stream_path(row,*info,piece);
                if (!job) {
                    auto found=reads_.find(key);
                    if (found!=reads_.end() && found->second->handle==handle) job=found->second;
                    else {
                        if (reads_.size()>=16) throw Error(429,"too many pending piece reads; retry later");
                        job=std::make_shared<PieceRead>(); job->handle=handle; job->created=Clock::now(); reads_[key]=job;
                        handle.read_piece(lt::piece_index_t(piece));
                    }
                }
            }
            if (job && job->ready) {
                if (reads_.contains(key) && reads_.at(key)==job) reads_.erase(key);
                if (job->error) throw Error(job->error,job->error==409?"piece checksum failed; pause and recheck this task":"verified piece read failed; retry or recheck");
                validate_stream_path(row,*handle.torrent_file(),piece);
                return job->bytes;
            }
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    throw Error(503,"requested piece is not ready; resume task and retry");
}
Json Bt::file_list(const httplib::Request& request) {
    auto row=owned(request,false); auto who=auth_.require(request); auto id=row["id"].get<long long>();
    if (!handles_.contains(id)) throw Error(409,"task unavailable");
    auto handle=handles_.at(id); auto info=handle.torrent_file(); auto state=handle.status(lt::torrent_handle::query_pieces);
    Json files=Json::array();
    for (auto index:info->files().file_range()) {
        auto name=info->files().file_path(index); if (!video_format(format_of(name))) continue;
        long long length=info->files().file_size(index),offset=info->files().file_offset(index),available=0,prefix=0; bool contiguous=true,integrity_error=false;
        int first=static_cast<int>(offset/info->piece_length()),last=length?static_cast<int>((offset+length-1)/info->piece_length()):first-1;
        for (int i=first;i<=last;++i) {
            auto begin=std::max(offset,static_cast<long long>(i)*info->piece_length());
            auto end=std::min(offset+length,static_cast<long long>(i+1)*info->piece_length());
            bool bad=quarantined_.contains({id,i}); integrity_error=integrity_error || bad;
            bool ready=present(state,i) && !bad;
            if (ready) { available+=end-begin; if (contiguous) prefix+=end-begin; } else contiguous=false;
        }
        auto history=db_.query("SELECT position FROM bt_history WHERE user_id=? AND task_id=? AND file_index=?",{who.id,id,static_cast<int>(index)});
        files.push_back({{"index",static_cast<int>(index)},{"name",name},{"format",format_of(name)},{"size",length},{"torrent_offset",offset},{"verified_bytes",available},{"contiguous_from_start",prefix},{"complete",available==length && checked(state)},{"integrity_error",integrity_error},{"position",history.empty()?Json(0.0):history[0]["position"]}});
    }
    return {{"files",files},{"task_id",id},{"paused",row["paused"]==1},{"piece_length",info->piece_length()},{"stream_wait_seconds",2},{"open_range_limit_bytes",window_bytes}};
}
void Bt::serve_stream(const httplib::Request& request,httplib::Response& response,bool invalid) {
    long long id=0; lt::torrent_handle handle; std::shared_ptr<const lt::torrent_info> info; size_t size=0,base=0; std::string name;
    {
        std::lock_guard lock(mutex_); auto row=owned(request,false); id=row["id"].get<long long>();
        if (!handles_.contains(id)) throw Error(409,"task unavailable");
        handle=handles_.at(id); info=handle.torrent_file(); int index=file_index(request,*info);
        auto file=lt::file_index_t(index); size=static_cast<size_t>(info->files().file_size(file)); base=static_cast<size_t>(info->files().file_offset(file)); name=info->files().file_path(file);
    }
    Slice selected;
    try { selected=slice(request,size,invalid); }
    catch (const Error& error) { clear_ranges(request); response.set_header("Content-Range","bytes */"+std::to_string(size)); reply(response,{{"error",error.what()}},416); return; }
    clear_ranges(request); response.set_header("Accept-Ranges","bytes");
    response.set_header("Content-Disposition","inline; filename*=UTF-8''"+encoded_filename(fs::path(name).filename().string()));
    if (request.method=="HEAD") { response.status=200; response.set_header("Content-Length",std::to_string(size)); response.set_header("Content-Type",type(name)); return; }
    if (request.has_header("If-Range")) throw Error(400,"BT streams have no stable validator; omit If-Range");
    if (!size) { response.set_content("",type(name)); return; }
    auto deadline=Clock::now()+std::chrono::seconds(2); auto first=static_cast<int>((base+selected.begin)/static_cast<size_t>(info->piece_length()));
    std::shared_ptr<const std::string> initial;
    try {
        initial=read_verified(request,id,handle,first,deadline);
        {
            std::lock_guard lock(mutex_); (void)owned(request,false);
            auto state=handle.status(lt::torrent_handle::query_pieces);
            if (selected.open) {
                auto end=std::min(size,selected.begin+window_bytes); auto absolute_end=base+end;
                for (int i=first+1;static_cast<size_t>(i)*static_cast<size_t>(info->piece_length())<absolute_end;++i) {
                    if (!present(state,i) || quarantined_.contains({id,i})) { end=static_cast<size_t>(i)*static_cast<size_t>(info->piece_length())-base; break; }
                }
                selected.length=end-selected.begin;
            } else {
                auto last=static_cast<int>((base+selected.begin+selected.length-1)/static_cast<size_t>(info->piece_length()));
                for (int i=first;i<=last;++i) if (!present(state,i) || quarantined_.contains({id,i})) throw Error(503,"range contains unavailable pieces; use an open byte range or retry");
            }
        }
    } catch (const Error& error) {
        if (error.status==503 || error.status==429) response.set_header("Retry-After","1");
        reply(response,{{"error",error.what()},{"task_id",id},{"first_piece",first},{"retryable",error.status==503 || error.status==429}},error.status); return;
    }
    response.status=selected.partial?206:200;
    if (selected.partial) response.set_header("Content-Range","bytes "+std::to_string(selected.begin)+"-"+std::to_string(selected.begin+selected.length-1)+"/"+std::to_string(size));
    response.set_header("X-BT-Verified-Read","piece-sha1-snapshot");
    auto snapshot=std::make_shared<httplib::Request>(request);
    response.set_content_provider(selected.length,type(name),[this,snapshot,id,handle,info,base,selected,bytes=initial,current_piece=first](size_t offset,size_t remaining,httplib::DataSink& sink) mutable {
        if (stopping_ || offset>=selected.length || !remaining) return false;
        try {
            auto absolute=base+selected.begin+offset; int piece=static_cast<int>(absolute/static_cast<size_t>(info->piece_length()));
            if (piece!=current_piece) { bytes=read_verified(*snapshot,id,handle,piece,Clock::now()+std::chrono::seconds(2)); current_piece=piece; }
            {
                std::lock_guard lock(mutex_); auto row=owned(*snapshot,false);
                if (!handles_.contains(id) || handles_.at(id)!=handle) return false;
                validate_stream_path(row,*info,piece);
            }
            auto within=absolute%static_cast<size_t>(info->piece_length());
            auto count=std::min({remaining,selected.length-offset,bytes->size()-within,size_t(65536)});
            return sink.write(bytes->data()+within,count);
        } catch (...) { log("bt_stream_aborted",{{"task_id",id}}); return false; }
    });
}
void Bt::range_failure(const httplib::Request& request,httplib::Response& response) {
    if (!request.path.starts_with("/bt/")) return;
    // Range parser failures occur before route matching in the HTTP library.
    httplib::Request matched=request;
    static const std::regex route("/bt/([1-9][0-9]{0,17})/file/([0-9]{1,3})");
    if (!std::regex_match(matched.path,matched.matches,route)) { reply(response,{{"error","stream not found"}},404); return; }
    try { serve_stream(matched,response,true); }
    catch (const Error& error) { clear_ranges(request); reply(response,{{"error",error.what()}},error.status); }
}
void Bt::streaming_routes(httplib::Server& server) {
    const std::string task="/api/tasks/([1-9][0-9]{0,17})";
    server.Get(task+"/files",[this](const httplib::Request& request,httplib::Response& response) { std::lock_guard lock(mutex_); reply(response,file_list(request)); });
    server.Get(task+"/schedule",[this](const httplib::Request& request,httplib::Response& response) { std::lock_guard lock(mutex_); auto row=owned(request,false); reply(response,schedule_status(row["id"].get<long long>())); });
    server.Post(task+"/focus",[this](const httplib::Request& request,httplib::Response& response) {
        if (request.body.size()>4096) throw Error(413,"focus request too large");
        std::lock_guard lock(mutex_); auto row=owned(request,true); auto input=body(request); auto id=row["id"].get<long long>();
        if (!handles_.contains(id)) throw Error(409,"task unavailable");
        auto handle=handles_.at(id); auto info=handle.torrent_file();
        if (input.size()!=2 || !input.contains("file_index") || !input.contains("byte_offset") || !input["file_index"].is_number_integer() || !input["byte_offset"].is_number_integer()) throw Error(400,"integer file_index and byte_offset required");
        auto index=input["file_index"].get<long long>(),offset=input["byte_offset"].get<long long>();
        if (index<0 || index>=info->files().num_files() || offset<0 || offset>=info->files().file_size(lt::file_index_t(static_cast<int>(index)))) throw Error(400,"file or byte offset outside torrent");
        auto absolute=info->files().file_offset(lt::file_index_t(static_cast<int>(index)))+offset;
        int piece=static_cast<int>(absolute/info->piece_length()); focus(id,handle,piece);
        auto output=schedule_status(id); output["mapped_piece"]=piece; output["offset_in_piece"]=absolute%info->piece_length(); reply(response,output);
    });
    server.Post(task+"/files/([0-9]{1,3})/history",[this](const httplib::Request& request,httplib::Response& response) {
        if (request.body.size()>4096) throw Error(413,"history request too large");
        std::lock_guard lock(mutex_); Transaction tx(db_); auto row=owned(request,true); auto who=auth_.require(request,false,true); auto id=row["id"].get<long long>();
        if (!handles_.contains(id)) throw Error(409,"task unavailable");
        auto info=handles_.at(id).torrent_file(); int index=file_index(request,*info); auto input=body(request);
        if (input.size()!=1 || !input.contains("position") || !input["position"].is_number()) throw Error(400,"position seconds required");
        double position=input["position"].get<double>(); if (!std::isfinite(position) || position<0 || position>30*86400) throw Error(400,"invalid playback position");
        db_.query("INSERT INTO bt_history(user_id,task_id,file_index,position,updated) VALUES(?,?,?,?,?) ON CONFLICT(user_id,task_id,file_index) DO UPDATE SET position=excluded.position,updated=excluded.updated",{who.id,id,index,position,now()}); tx.commit(); reply(response,{{"saved",true}});
    });
    server.Get("/bt/([1-9][0-9]{0,17})/file/([0-9]{1,3})",[this](const httplib::Request& request,httplib::Response& response) { serve_stream(request,response); });
}
}
