#include "shares.hpp"
#include "remote.hpp"
#include <charconv>
#include <fstream>
#include <limits>
#include <unistd.h>
namespace lv {
namespace {
Json input(const httplib::Request& request,std::initializer_list<const char*> fields) {
    if (request.body.size()>4096) throw Error(413,"share request too large");
    auto type=request.get_header_value("Content-Type");
    if (request.get_header_value_count("Content-Type")!=1 || (type!="application/json" && !type.starts_with("application/json;"))) throw Error(415,"application/json required");
    auto value=body(request);
    for (auto it=value.begin();it!=value.end();++it)
        if (std::none_of(fields.begin(),fields.end(),[&](const char* key){return it.key()==key;})) throw Error(400,"unknown share field");
    return value;
}
long long integer(const Json& value,const char* key,long long low,long long high) {
    if (!value.contains(key) || !value[key].is_number_integer() || (value[key].is_number_unsigned() && value[key].get<unsigned long long>()>static_cast<unsigned long long>(high))) throw Error(400,std::string(key)+" integer required");
    auto result=value[key].get<long long>();
    if (result<low || result>high) throw Error(400,std::string(key)+" outside allowed range");
    return result;
}
void no_query(const httplib::Request& request) { if (!request.params.empty()) throw Error(400,"share endpoint takes no query parameters"); }
}
Shares::Shares(Database& database,Auth& auth,Media& media,const Config& config):database_(database),auth_(auth),media_(media),config_(config) {
    Transaction tx(database_);
    database_.query("CREATE TABLE IF NOT EXISTS shares(id INTEGER PRIMARY KEY AUTOINCREMENT,media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,creator INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,token_hash TEXT NOT NULL UNIQUE,scope TEXT NOT NULL CHECK(scope='video_read'),created INTEGER NOT NULL,expires INTEGER NOT NULL,revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN(0,1)))");
    database_.query("CREATE TABLE IF NOT EXISTS share_sessions(token_hash TEXT PRIMARY KEY,share_id INTEGER NOT NULL REFERENCES shares(id) ON DELETE CASCADE,expires INTEGER NOT NULL)");
    database_.query("CREATE INDEX IF NOT EXISTS share_sessions_share ON share_sessions(share_id)");
    database_.query("DELETE FROM share_sessions WHERE expires<=?",{now()});
    tx.commit();
}
std::string Shares::cookie(const std::string& value,long long ttl) const {
    return "lv_share="+value+"; Path=/share/; HttpOnly; SameSite=Strict; Max-Age="+std::to_string(ttl)+(config_.public_origin.empty() ? "" : "; Secure");
}
std::string Shares::session(const httplib::Request& request) const {
    no_query(request);
    if (request.get_header_value_count("Cookie")>1 || request.has_header("Authorization")) throw Error(400,"ambiguous share credentials");
    auto text=request.get_header_value("Cookie"); size_t position=0; std::string token; int found=0;
    while (position<text.size()) {
        auto end=text.find(';',position); if (end==std::string::npos) end=text.size();
        auto pair=text.substr(position,end-position); auto first=pair.find_first_not_of(" \t");
        if (first!=std::string::npos) pair.erase(0,first);
        if (pair.starts_with("lv_share=")) { token=pair.substr(9); ++found; }
        position=end+1;
    }
    if (found>1) throw Error(400,"ambiguous share credentials");
    if (found!=1 || !valid_secret(token)) throw Error(401,"Open a valid share link to start playback");
    return secret_hash(token);
}
Json Shares::resolve(const std::string& hash) {
    std::lock_guard lock(database_.mutex());
    auto rows=database_.query("SELECT m.*,s.expires AS share_expires FROM share_sessions ss JOIN shares s ON s.id=ss.share_id JOIN users u ON u.id=s.creator JOIN media m ON m.id=s.media_id WHERE ss.token_hash=? AND ss.expires>? AND s.expires>? AND s.revoked=0 AND s.scope='video_read' AND u.role='admin' AND u.disabled=0 AND m.root=?",{hash,now(),now(),media_.root()});
    if (rows.empty()) throw Error(404,"Share link expired, revoked or unavailable");
    return rows[0];
}
void Shares::serve(const httplib::Request& request,httplib::Response& response,bool invalid_range) {
    auto hash=session(request);
    media_.serve_authorized(request,response,[this,hash]{return resolve(hash);},invalid_range);
}
void Shares::range_failure(const httplib::Request& request,httplib::Response& response) {
    if (request.path!="/share/media") return;
    try { serve(request,response,true); }
    catch (const Error& error) { const_cast<httplib::Request&>(request).ranges.clear(); reply(response,{{"error",error.what()}},error.status); }
}
void Shares::routes(httplib::Server& server) {
    server.Post("/api/shares",[this](const auto& request,auto& response) {
        Transaction tx(database_); auto who=auth_.require(request,true,true);
        auto value=input(request,{"media_id","ttl_seconds","scope"});
        auto id=integer(value,"media_id",1,std::numeric_limits<long long>::max()),ttl=integer(value,"ttl_seconds",30,604800);
        if (value.value("scope",Json("video_read"))!="video_read") throw Error(400,"only single-video video_read scope supported");
        auto row=media_.lookup(id,who);
        auto file=regular_file(directory(row["root"].template get<std::string>()),row["relative"].template get<std::string>());
        auto stat=file->stat();
        if (stat.st_size!=row["size"] || static_cast<long long>(stat.st_ino)!=row["inode"] || static_cast<long long>(stat.st_dev)!=row["device"] || modified_ns(stat)!=row["modified"]) throw Error(409,"media changed; rescan first");
        if (database_.query("SELECT count(*) AS n FROM shares WHERE revoked=0 AND expires>?",{now()})[0]["n"].template get<int>()>=1000) throw Error(409,"active share limit reached");
        auto token=random_secret(); auto expires=now()+ttl;
        auto share=database_.insert("INSERT INTO shares(media_id,creator,token_hash,scope,created,expires) VALUES(?,?,?,'video_read',?,?)",{id,who.id,secret_hash(token),now(),expires});
        database_.query("INSERT INTO audit(timestamp,actor,event,detail) VALUES(?,?,'share_created',?)",{now(),who.id,Json({{"share_id",share},{"media_id",id},{"expires",expires}}).dump()});
        tx.commit(); log("share_created",{{"actor",who.id},{"share_id",share},{"media_id",id}});
        auto origin=config_.remote_mode=="reverse_proxy" ? config_.public_origin : "";
        reply(response,{{"id",share},{"media_id",id},{"scope","video_read"},{"expires",expires},{"url",origin+"/share#"+token},{"transport",config_.remote_mode=="reverse_proxy" ? "https_entry" : "local_http_only"}},201);
    });
    server.Get("/api/shares",[this](const auto& request,auto& response) {
        auth_.require(request,true); no_query(request);
        auto rows=database_.query("SELECT s.id,s.media_id,m.name,s.creator,s.scope,s.created,s.expires,s.revoked FROM shares s JOIN media m ON m.id=s.media_id ORDER BY s.id DESC LIMIT 200");
        reply(response,{{"shares",rows}});
    });
    server.Delete(R"(/api/shares/(\d+))",[this](const httplib::Request& request,httplib::Response& response) {
        Transaction tx(database_); auto who=auth_.require(request,true,true); no_query(request);
        auto text=request.matches[1].str(); long long id=0; auto parsed=std::from_chars(text.data(),text.data()+text.size(),id);
        if (parsed.ec!=std::errc{} || parsed.ptr!=text.data()+text.size() || id<1) throw Error(400,"invalid share id");
        if (database_.query("SELECT id FROM shares WHERE id=?",{id}).empty()) throw Error(404,"share not found");
        database_.query("UPDATE shares SET revoked=1 WHERE id=?",{id}); database_.query("DELETE FROM share_sessions WHERE share_id=?",{id});
        database_.query("INSERT INTO audit(timestamp,actor,event,detail) VALUES(?,?,'share_revoked',?)",{now(),who.id,Json({{"share_id",id}}).dump()});
        tx.commit(); log("share_revoked",{{"actor",who.id},{"share_id",id}}); reply(response,{{"ok",true}});
    });
    server.Post("/share/api/exchange",[this](const auto& request,auto& response) {
        no_query(request);
        validate_origin(request,config_);
        auto value=input(request,{"token"}); auto token=value.value("token",std::string());
        if (!valid_secret(token)) throw Error(404,"Share link expired, revoked or unavailable");
        Transaction tx(database_);
        auto rows=database_.query("SELECT s.id,s.expires FROM shares s JOIN users u ON u.id=s.creator JOIN media m ON m.id=s.media_id WHERE s.token_hash=? AND s.expires>? AND s.revoked=0 AND u.disabled=0 AND u.role='admin' AND m.root=?",{secret_hash(token),now(),media_.root()});
        if (rows.empty()) throw Error(404,"Share link expired, revoked or unavailable");
        database_.query("DELETE FROM share_sessions WHERE expires<=?",{now()});
        auto share_id=rows[0]["id"].template get<long long>();
        if (database_.query("SELECT count(*) AS n FROM share_sessions WHERE share_id=?",{share_id})[0]["n"].template get<int>()>=64 || database_.query("SELECT count(*) AS n FROM share_sessions")[0]["n"].template get<int>()>=4096) throw Error(429,"share session limit reached; retry after sessions expire");
        auto raw=random_secret(); auto expires=std::min(rows[0]["expires"].template get<long long>(),now()+3600);
        database_.query("INSERT INTO share_sessions(token_hash,share_id,expires) VALUES(?,?,?)",{secret_hash(raw),share_id,expires});
        tx.commit(); response.set_header("Set-Cookie",cookie(raw,expires-now())); reply(response,{{"ok",true},{"expires",expires}});
    });
    server.Get("/share/api/info",[this](const auto& request,auto& response) {
        auto row=resolve(session(request));
        reply(response,{{"name",row["name"]},{"size",row["size"]},{"format",row["format"]},{"duration",row["duration"]},{"scope","video_read"},{"expires",row["share_expires"]}});
    });
    server.Get("/share/media",[this](const auto& request,auto& response){serve(request,response);});
    for (const auto& entry:std::vector<std::pair<std::string,std::string>>{{"/share","share.html"},{"/share/app.js","share.js"}}) {
        auto folder=directory(config_.web); auto name=entry.second;
        auto type=entry.second.ends_with(".html") ? "text/html; charset=utf-8" : "text/javascript; charset=utf-8";
        server.Get(entry.first,[folder,name,type](const auto&,auto& response){
            auto file=regular_file(folder,name); auto stat=file->stat();
            if (stat.st_size>2*1024*1024) throw Error(500,"web asset too large");
            std::string content(static_cast<size_t>(stat.st_size),'\0');
            if (::pread(file->fd,content.data(),content.size(),0)!=stat.st_size) throw Error(500,"web asset read failed");
            response.set_content(content,type);
        });
    }
}
}
