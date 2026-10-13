#include "core.hpp"
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <regex>
#include <arpa/inet.h>
#include <sys/stat.h>
#include <array>
#include <set>
#include <fcntl.h>
#include <unistd.h>
#include "files.hpp"
namespace lv {
static std::string configured_log_level = "info";
static std::string variable(const char* key, const std::string& fallback) {
    const char* v = std::getenv(key); return v ? std::string(v) : fallback;
}
Config Config::load() {
    Config c;
    c.bind = variable("LANVIDEO_BIND", "127.0.0.1");
    in_addr address{};
    if (inet_pton(AF_INET, c.bind.c_str(), &address) != 1) throw Error(400,"bind must be an IPv4 address");
    auto port = variable("LANVIDEO_PORT", "8080");
    size_t consumed = 0;
    c.port = std::stoi(port, &consumed);
    if (consumed != port.size() || c.port < 1 || c.port > 65535) throw Error(400, "invalid port");
    auto ttl = variable("LANVIDEO_SESSION_TTL", "28800");
    consumed = 0;
    c.session_ttl = std::stoi(ttl, &consumed);
    if (consumed != ttl.size() || c.session_ttl < 30 || c.session_ttl > 86400) throw Error(400,"session TTL must be 30 to 86400 seconds");
    auto limit=variable("LANVIDEO_UPLOAD_MIB","512"); consumed=0;
    auto mib=std::stoul(limit,&consumed);
    if (consumed!=limit.size() || mib<1 || mib>4096) throw Error(400,"upload limit must be 1 to 4096 MiB");
    c.upload_limit=static_cast<size_t>(mib)*1024*1024;
    c.ffprobe=variable("LANVIDEO_FFPROBE","/usr/bin/ffprobe");
    if (!c.ffprobe.empty() && !fs::path(c.ffprobe).is_absolute()) throw Error(400,"ffprobe path must be absolute or empty");
    c.data = fs::absolute(variable("LANVIDEO_DATA", "var")).lexically_normal();
    (void)private_directory(c.data);
    auto web=variable("LANVIDEO_WEB_ROOT","");
    if (!web.empty()) {
        if (!fs::path(web).is_absolute()) throw Error(400,"web root must be absolute");
        c.web=fs::path(web).lexically_normal();
    } else {
        std::array<char,4096> executable{};
        auto count=::readlink("/proc/self/exe",executable.data(),executable.size()-1);
        if (count<0) throw Error(500,"cannot locate executable");
        auto installed=fs::path(std::string(executable.data(),static_cast<size_t>(count))).parent_path().parent_path()/"share/lanvideo/web";
        c.web=fs::exists(installed) ? installed : fs::path(LANVIDEO_WEB_DIR);
    }
    auto assets=directory(c.web);
    for (const auto* name:{"index.html","app.js","style.css","sha256.min.js","share.html","share.js"})
        (void)regular_file(assets,name);
    c.public_origin = variable("LANVIDEO_PUBLIC_ORIGIN", "");
    if (!c.public_origin.empty() && !std::regex_match(c.public_origin, std::regex(R"(https://[A-Za-z0-9.-]+(:[0-9]{1,5})?)")))
        throw Error(400,"public origin must be an HTTPS origin without a path");
    if (!c.public_origin.empty()) {
        auto authority=c.public_origin.substr(8); auto colon=authority.find(':');
        auto host=authority.substr(0,colon);
        if (host.size()>253 || host.front()=='.' || host.back()=='.' || host.find("..")!=std::string::npos)
            throw Error(400,"invalid public origin hostname");
        if (colon!=std::string::npos) {
            auto number=std::stoul(authority.substr(colon+1));
            if (number<1 || number>65535) throw Error(400,"invalid public origin port");
        }
    }
    c.remote_mode=variable("LANVIDEO_REMOTE_MODE","local");
    c.remote_ca=variable("LANVIDEO_REMOTE_CA","");
    if (!c.remote_ca.empty() && !fs::path(c.remote_ca).is_absolute()) throw Error(400,"remote CA path must be absolute");
    if (c.remote_mode!="local" && c.remote_mode!="reverse_proxy") throw Error(400,"remote mode must be local or reverse_proxy");
    if (c.remote_mode=="reverse_proxy" && (c.bind!="127.0.0.1" || c.public_origin.empty()))
        throw Error(400,"reverse proxy requires loopback bind and HTTPS public origin");
    c.log_level = variable("LANVIDEO_LOG_LEVEL", "info");
    if (c.log_level != "info" && c.log_level != "error") throw Error(400,"log level must be info or error");
    configured_log_level = c.log_level;
    return c;
}
long long now() { return std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch()).count(); }
void log(const std::string& event, const Json& fields, const std::string& level) {
    if (configured_log_level == "error" && level != "error") return;
    static std::mutex guard;
    std::lock_guard lock(guard);
    std::cout << Json({{"time",now()},{"level",level},{"event",event},{"fields",fields}}).dump() << std::endl;
}
void reply(httplib::Response& r, const Json& j, int status) { r.status = status; r.set_content(j.dump(), "application/json; charset=utf-8"); }
Json body(const httplib::Request& r, size_t limit) {
    if (r.body.size()>limit) throw Error(413,"JSON body too large");
    std::vector<std::set<std::string>> keys;
    auto validate=[&](int depth,Json::parse_event_t event,Json& value) {
        if (depth>32) throw Error(400,"JSON nesting limit exceeded");
        if (event==Json::parse_event_t::object_start) keys.emplace_back();
        else if (event==Json::parse_event_t::object_end) keys.pop_back();
        else if (event==Json::parse_event_t::key && !keys.back().insert(value.get<std::string>()).second) throw Error(400,"duplicate JSON key");
        return true;
    };
    try { auto j = Json::parse(r.body,validate); if (!j.is_object()) throw Error(400,"expected JSON object"); return j; }
    catch (const Json::exception&) { throw Error(400,"invalid JSON"); }
}
void validate_origin(const httplib::Request& request,const Config& config) {
    if (request.get_header_value_count("Origin")>1) throw Error(403,"ambiguous origin");
    if (!request.has_header("Origin")) return;
    std::string expected=config.public_origin;
    if (expected.empty()) {
        if (request.get_header_value_count("Host")!=1) throw Error(403,"one host header required");
        expected="http://"+request.get_header_value("Host");
    }
    if (request.get_header_value("Origin")!=expected) throw Error(403,"untrusted origin");
}
void reject_duplicate_query(const httplib::Request& request) {
    // Pinned httplib deduplicates identical raw pairs while parsing params.
    // Validate the original target so repeated identical keys cannot disappear.
    auto question=request.target.find('?'); if (question==std::string::npos) return;
    std::set<std::string> seen; auto start=question+1;
    while (start<request.target.size()) {
        auto end=request.target.find('&',start); if (end==std::string::npos) end=request.target.size();
        auto part=request.target.substr(start,end-start); auto equal=part.find('=');
        if (!part.empty() && !seen.insert(httplib::detail::decode_url(part.substr(0,equal),true)).second) throw Error(400,"duplicate query parameter");
        start=end+1;
    }
}
Database::Database(const fs::path& file) {
    auto parent=directory(file.parent_path());
    auto name=file.filename().string();
    for (const auto& suffix:{std::string(""),std::string("-wal"),std::string("-shm"),std::string("-journal")}) {
        struct stat stat{};
        if (::fstatat(parent->fd,(name+suffix).c_str(),&stat,AT_SYMLINK_NOFOLLOW)==0) {
            if (!S_ISREG(stat.st_mode) || stat.st_nlink!=1 || stat.st_uid!=::geteuid() || (stat.st_mode&077)!=0)
                throw Error(403,"database files must be owned private regular files without links");
        } else if (errno!=ENOENT) throw Error(500,"cannot inspect database files");
    }
    if (sqlite3_open_v2(file.c_str(), &handle_, SQLITE_OPEN_READWRITE|SQLITE_OPEN_CREATE|SQLITE_OPEN_FULLMUTEX|SQLITE_OPEN_NOFOLLOW, nullptr) != SQLITE_OK) {
        if (handle_) sqlite3_close(handle_);
        throw Error(500,"database open failed");
    }
    try {
    sqlite3_busy_timeout(handle_, 5000);
    query("PRAGMA journal_mode=WAL");
    query("PRAGMA foreign_keys=ON");
    query("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)");
    query("CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, timestamp INTEGER NOT NULL, actor INTEGER, event TEXT NOT NULL, detail TEXT NOT NULL)");
    } catch (...) { sqlite3_close(handle_); handle_ = nullptr; throw; }
}
Database::~Database() { if (handle_) sqlite3_close(handle_); }
Json Database::query(const std::string& sql, const Json& p) {
    std::lock_guard lock(mutex_);
    if (!p.is_array()) throw Error(500,"database parameter list must be an array");
    sqlite3_stmt* raw = nullptr;
    if (sqlite3_prepare_v2(handle_, sql.c_str(), -1, &raw, nullptr) != SQLITE_OK) throw Error(500,"database prepare failed");
    std::unique_ptr<sqlite3_stmt, decltype(&sqlite3_finalize)> stmt(raw, sqlite3_finalize);
    for (size_t i = 0; i < p.size(); ++i) {
        int rc;
        int idx = static_cast<int>(i + 1);
        if (p[i].is_null()) rc = sqlite3_bind_null(raw, idx);
        else if (p[i].is_number_integer()) rc = sqlite3_bind_int64(raw, idx, p[i].get<long long>());
        else if (p[i].is_number_float()) rc = sqlite3_bind_double(raw, idx, p[i].get<double>());
        else if (p[i].is_string()) { auto s = p[i].get<std::string>(); rc = sqlite3_bind_text(raw, idx, s.c_str(), static_cast<int>(s.size()), SQLITE_TRANSIENT); }
        else throw Error(500,"unsupported database parameter type at index "+std::to_string(idx));
        if (rc != SQLITE_OK) throw Error(500,"database binding failed");
    }
    Json rows = Json::array();
    int rc;
    while ((rc = sqlite3_step(raw)) == SQLITE_ROW) {
        Json row = Json::object();
        for (int i=0; i<sqlite3_column_count(raw); ++i) {
            auto name = sqlite3_column_name(raw,i);
            switch (sqlite3_column_type(raw,i)) {
                case SQLITE_NULL: row[name]=nullptr; break;
                case SQLITE_INTEGER: row[name]=sqlite3_column_int64(raw,i); break;
                case SQLITE_FLOAT: row[name]=sqlite3_column_double(raw,i); break;
                default: row[name]=std::string(reinterpret_cast<const char*>(sqlite3_column_text(raw,i)),static_cast<size_t>(sqlite3_column_bytes(raw,i)));
            }
        }
        rows.push_back(row);
    }
    if (rc != SQLITE_DONE) throw Error(409,"database constraint or execution failure");
    return rows;
}
long long Database::insert(const std::string& sql, const Json& p) {
    std::lock_guard lock(mutex_); query(sql,p); return sqlite3_last_insert_rowid(handle_);
}
Transaction::Transaction(Database& database): database_(database), lock_(database.mutex()) {
    database_.query("BEGIN IMMEDIATE");
}
Transaction::~Transaction() {
    if (!committed_) {
        try { database_.query("ROLLBACK"); }
        catch (...) { log("database_rollback_failure",Json::object(),"error"); }
    }
}
void Transaction::commit() { database_.query("COMMIT"); committed_ = true; lock_.unlock(); }
}
