#include "auth.hpp"
#include <sodium.h>
#include <algorithm>
#include <array>
#include <charconv>
#include <iostream>
#include <regex>
#include <termios.h>
#include <unistd.h>
namespace lv {
namespace {
std::string digest(const std::string& input) {
    std::array<unsigned char,crypto_hash_sha256_BYTES> hash{};
    crypto_hash_sha256(hash.data(),reinterpret_cast<const unsigned char*>(input.data()),input.size());
    std::array<char,crypto_hash_sha256_BYTES*2+1> hex{};
    sodium_bin2hex(hex.data(),hex.size(),hash.data(),hash.size());
    return hex.data();
}
std::string random_token() {
    std::array<unsigned char,32> bytes{};
    std::array<char,65> hex{};
    randombytes_buf(bytes.data(),bytes.size());
    sodium_bin2hex(hex.data(),hex.size(),bytes.data(),bytes.size());
    return hex.data();
}
void fields(const Json& input, std::initializer_list<const char*> allowed) {
    if (!input.is_object()) throw Error(400,"expected JSON object");
    for (auto it=input.begin(); it!=input.end(); ++it)
        if (std::none_of(allowed.begin(),allowed.end(),[&](const char* key){ return it.key()==key; })) throw Error(400,"unknown input field");
}
std::string text(const Json& input, const char* key) {
    if (!input.contains(key) || !input[key].is_string()) throw Error(400,"missing or invalid text field");
    return input[key].get<std::string>();
}
std::string username(const Json& input) {
    auto value=text(input,"username");
    if (!std::regex_match(value,std::regex("[A-Za-z0-9_.-]{3,64}"))) throw Error(400,"username must be 3 to 64 ASCII letters, digits, dots, underscores or hyphens");
    return value;
}
std::string new_password(const Json& input, const char* key="password") {
    auto value=text(input,key);
    if (value.size()<12 || value.size()>1024 || value.find('\0')!=std::string::npos) throw Error(400,"password must be 12 to 1024 bytes without NUL");
    return value;
}
std::string role_value(const Json& input) {
    auto role=input.contains("role") ? text(input,"role") : "user";
    if (role!="admin" && role!="user") throw Error(400,"role must be admin or user");
    return role;
}
Json input_body(const httplib::Request& request) {
    if (request.body.size()>4096) throw Error(413,"authentication request too large");
    const auto type=request.get_header_value("Content-Type");
    if (type!="application/json" && !type.starts_with("application/json;")) throw Error(415,"application/json required");
    return body(request);
}
long long route_id(const httplib::Request& request) {
    auto str=request.matches[1].str();
    long long value=0;
    auto result=std::from_chars(str.data(),str.data()+str.size(),value);
    if (result.ec!=std::errc{} || result.ptr!=str.data()+str.size() || value<=0) throw Error(400,"invalid user id");
    return value;
}
bool equal_secret(const std::string& left, const std::string& right) {
    return left.size()==right.size() && sodium_memcmp(left.data(),right.data(),left.size())==0;
}
class HashSlot {
    std::counting_semaphore<2>& slots_;
public:
    explicit HashSlot(std::counting_semaphore<2>& slots): slots_(slots) {
        if (!slots_.try_acquire()) throw Error(429,"password service busy; retry later");
    }
    ~HashSlot() { slots_.release(); }
};
std::string hidden_line(const char* prompt) {
    termios original{};
    if (tcgetattr(STDIN_FILENO,&original)!=0) throw Error(400,"interactive initialization needs a terminal; use --init-admin-stdin for a secure pipe");
    auto hidden=original; hidden.c_lflag &= static_cast<tcflag_t>(~ECHO);
    if (tcsetattr(STDIN_FILENO,TCSAFLUSH,&hidden)!=0) throw Error(500,"cannot hide password input");
    struct RestoreTerminal { termios mode; ~RestoreTerminal() { tcsetattr(STDIN_FILENO,TCSANOW,&mode); } } restore{original};
    std::cerr << prompt << std::flush;
    std::string line;
    bool ok=static_cast<bool>(std::getline(std::cin,line));
    std::cerr << '\n';
    if (!ok) throw Error(400,"password input cancelled");
    return line;
}
}
Json Identity::public_user() const { return {{"id",id},{"username",username},{"role",role}}; }
Auth::Auth(Database& database,const Config& config):database_(database),config_(config) {
    Transaction transaction(database_);
    database_.query("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('admin','user')),disabled INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0,1)))");
    database_.query("CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,expires INTEGER NOT NULL,created INTEGER NOT NULL)");
    database_.query("CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id)");
    database_.query("CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires)");
    transaction.commit();
    dummy_hash_=password_hash(random_token());
}
bool Auth::initialized() {
    auto rows=database_.query("SELECT value FROM settings WHERE key='auth_initialized'");
    return !rows.empty();
}
std::string Auth::password_hash(const std::string& password) {
    HashSlot slot(hash_slots_);
    std::array<char,crypto_pwhash_STRBYTES> hash{};
    if (crypto_pwhash_str_alg(hash.data(),password.data(),password.size(),crypto_pwhash_OPSLIMIT_INTERACTIVE,crypto_pwhash_MEMLIMIT_INTERACTIVE,crypto_pwhash_ALG_ARGON2ID13)!=0)
        throw Error(503,"password hashing memory unavailable");
    return hash.data();
}
bool Auth::verify(const std::string& encoded,const std::string& password) {
    HashSlot slot(hash_slots_);
    return crypto_pwhash_str_verify(encoded.c_str(),password.data(),password.size())==0;
}
void Auth::audit(const std::string& event,const Json& actor,const Json& detail) {
    database_.query("INSERT INTO audit(timestamp,actor,event,detail) VALUES(?,?,?,?)",{now(),actor,event,detail.dump()});
}
void Auth::finish(Transaction& transaction,const std::string& event,const Json& actor,const Json& detail) {
    audit(event,actor,detail);
    transaction.commit();
    log(event,{{"actor",actor},{"detail",detail}});
}
void Auth::initialize(const Json& input) {
    fields(input,{"username","password"});
    auto name=username(input);
    auto hash=password_hash(new_password(input));
    Transaction transaction(database_);
    if (initialized() || !database_.query("SELECT id FROM users LIMIT 1").empty()) throw Error(409,"administrator has already been initialized");
    auto id=database_.insert("INSERT INTO users(username,password_hash,role) VALUES(?,?,'admin')",{name,hash});
    database_.query("INSERT INTO settings(key,value) VALUES('auth_initialized','1')");
    finish(transaction,"admin_initialized",id,Json::object());
}
void Auth::throttle(const std::string& address) {
    std::lock_guard lock(rate_mutex_);
    auto timestamp=now();
    for (auto it=login_windows_.begin(); it!=login_windows_.end();)
        if (timestamp-it->second.start>=60) it=login_windows_.erase(it); else ++it;
    if (!login_windows_.contains(address)) {
        if (login_windows_.size()>=1024) throw Error(429,"login rate limit capacity reached");
        login_windows_.emplace(address,Window{timestamp,0});
    }
    if (++login_windows_.at(address).count>20) throw Error(429,"too many login attempts; retry after one minute");
}
void Auth::origin(const httplib::Request& request) const {
    validate_origin(request,config_);
}
std::string Auth::cookie_header(const std::string& token,int lifetime) const {
    return "lv_session="+token+"; Path=/; HttpOnly; SameSite=Strict; Max-Age="+std::to_string(lifetime)+(config_.public_origin.empty() ? "" : "; Secure");
}
Identity Auth::require(const httplib::Request& request,bool admin,bool mutation) {
    if (request.get_header_value_count("Authorization")>1) throw Error(400,"ambiguous authorization");
    if (request.get_header_value_count("Cookie")>1) throw Error(400,"ambiguous cookie headers");
    std::string raw;
    bool cookie=false;
    auto authorization=request.get_header_value("Authorization");
    std::string cookies=request.get_header_value("Cookie");
    size_t offset=0;
    while (offset<cookies.size()) {
        auto end=cookies.find(';',offset);
        auto part=cookies.substr(offset,end==std::string::npos ? end : end-offset);
        auto begin=part.find_first_not_of(" \t");
        if (begin!=std::string::npos) part.erase(0,begin);
        if (part.starts_with("lv_session=")) {
            if (cookie) throw Error(400,"ambiguous session cookie");
            cookie=true; raw=part.substr(11);
        }
        if (end==std::string::npos) break;
        offset=end+1;
    }
    if (!authorization.empty()) {
        if (cookie) throw Error(400,"use cookie or bearer authentication, not both");
        if (!authorization.starts_with("Bearer ")) throw Error(401,"authentication required");
        raw=authorization.substr(7);
    }
    if (raw.size()!=64 || raw.find_first_not_of("0123456789abcdef")!=std::string::npos) throw Error(401,"authentication required");
    auto hash=digest(raw);
    auto rows=database_.query("SELECT u.id,u.username,u.role FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>? AND u.disabled=0",{hash,now()});
    if (rows.empty()) throw Error(401,"session invalid or expired");
    auto row=rows[0];
    Identity who{row["id"].get<long long>(),row["username"].get<std::string>(),row["role"].get<std::string>(),hash,digest("csrf:"+raw),cookie};
    if (admin && who.role!="admin") throw Error(403,"administrator role required");
    if (mutation) {
        origin(request);
        if (cookie && (request.get_header_value_count("X-CSRF-Token")!=1 || !equal_secret(request.get_header_value("X-CSRF-Token"),who.csrf))) throw Error(403,"CSRF token required");
    }
    return who;
}
void Auth::invalidate(long long id) {
    database_.query("DELETE FROM sessions WHERE user_id=?",{id});
    // Password/role/disable changes revoke delegated access permanently, too.
    // CLI bootstrap uses Auth before the optional sharing schema is initialized.
    if (!database_.query("SELECT name FROM sqlite_master WHERE type='table' AND name='shares'").empty()) {
        database_.query("UPDATE shares SET revoked=1 WHERE creator=?",{id});
        database_.query("DELETE FROM share_sessions WHERE share_id IN(SELECT id FROM shares WHERE creator=?)",{id});
    }
}
void Auth::protect_last_admin(const Json& old_user,const std::string& role,bool disabled) {
    if (old_user["role"]=="admin" && old_user["disabled"]==0 && (role!="admin" || disabled)) {
        auto count=database_.query("SELECT count(*) AS total FROM users WHERE role='admin' AND disabled=0")[0]["total"].get<long long>();
        if (count<=1) throw Error(409,"cannot remove the last enabled administrator");
    }
}
void Auth::routes(httplib::Server& server) {
    server.Get("/api/bootstrap/status",[this](const auto&,auto& response) { reply(response,{{"initialized",initialized()}}); });
    server.Post("/api/bootstrap",[](const auto&,auto&) { throw Error(405,"initialize administrator using the local CLI"); });
    server.Post("/api/login",[this](const auto& request,auto& response) {
        origin(request);
        auto input=input_body(request); fields(input,{"username","password"});
        auto name=username(input),password=text(input,"password");
        if (password.empty() || password.size()>1024 || password.find('\0')!=std::string::npos) throw Error(400,"invalid password length");
        // The global ingress guard validates this header only in reverse_proxy mode.
        throttle(config_.remote_mode=="reverse_proxy" ? request.get_header_value("X-Real-IP") : request.remote_addr);
        if (!initialized()) throw Error(409,"administrator initialization required");
        auto rows=database_.query("SELECT * FROM users WHERE username=?",{name});
        auto hash=rows.empty() ? dummy_hash_ : rows[0]["password_hash"].template get<std::string>();
        bool valid=verify(hash,password);
        if (rows.empty() || !valid || rows[0]["disabled"]!=0) {
            audit("login_failed",nullptr,Json::object()); log("login_failed"); throw Error(401,"invalid username or password");
        }
        auto id=rows[0]["id"].template get<long long>();
        auto token=random_token();
        auto expires=now()+config_.session_ttl;
        Transaction transaction(database_);
        auto current=database_.query("SELECT id,username,role,password_hash,disabled FROM users WHERE id=?",{id});
        if (current.empty() || current[0]["disabled"]!=0 || current[0]["password_hash"]!=hash) throw Error(401,"account changed; log in again");
        database_.query("DELETE FROM sessions WHERE expires<=?",{now()});
        database_.query("INSERT INTO sessions(token_hash,user_id,expires,created) VALUES(?,?,?,?)",{digest(token),id,expires,now()});
        database_.query("DELETE FROM sessions WHERE user_id=? AND token_hash NOT IN (SELECT token_hash FROM sessions WHERE user_id=? ORDER BY created DESC,rowid DESC LIMIT 10)",{id,id});
        finish(transaction,"login_succeeded",id,Json::object());
        response.set_header("Set-Cookie",cookie_header(token,config_.session_ttl));
        reply(response,{{"user",{{"id",id},{"username",current[0]["username"]},{"role",current[0]["role"]}}},{"csrf_token",digest("csrf:"+token)},{"expires",expires}});
    });
    server.Get("/api/me",[this](const auto& request,auto& response) {
        auto who=require(request); reply(response,{{"user",who.public_user()},{"csrf_token",who.csrf}});
    });
    server.Post("/api/logout",[this](const auto& request,auto& response) {
        Transaction transaction(database_); auto who=require(request,false,true);
        database_.query("DELETE FROM sessions WHERE token_hash=?",{who.token_hash});
        finish(transaction,"logout",who.id,Json::object());
        response.set_header("Set-Cookie",cookie_header("",0)); reply(response,{{"ok",true}});
    });
    server.Post("/api/password",[this](const auto& request,auto& response) {
        auto who=require(request,false,true);
        auto input=input_body(request); fields(input,{"old_password","new_password"});
        auto old=text(input,"old_password"),replacement=new_password(input,"new_password");
        if (old.size()>1024) throw Error(400,"invalid password length");
        auto rows=database_.query("SELECT password_hash FROM users WHERE id=?",{who.id});
        if (rows.empty()) throw Error(401,"session invalid or expired");
        auto original=rows[0]["password_hash"].template get<std::string>();
        if (!verify(original,old)) throw Error(403,"current password is incorrect");
        auto updated=password_hash(replacement);
        Transaction transaction(database_); who=require(request,false,true);
        if (database_.query("SELECT password_hash FROM users WHERE id=?",{who.id})[0]["password_hash"]!=original) throw Error(409,"password changed concurrently");
        database_.query("UPDATE users SET password_hash=? WHERE id=?",{updated,who.id}); invalidate(who.id);
        finish(transaction,"password_changed",who.id,Json::object());
        response.set_header("Set-Cookie",cookie_header("",0)); reply(response,{{"ok",true},{"login_required",true}});
    });
    server.Get("/api/users",[this](const auto& request,auto& response) {
        std::lock_guard lock(database_.mutex()); require(request,true);
        reply(response,{{"users",database_.query("SELECT id,username,role,disabled FROM users ORDER BY id")}});
    });
    server.Post("/api/users",[this](const auto& request,auto& response) {
        require(request,true,true); auto input=input_body(request); fields(input,{"username","password","role"});
        auto name=username(input),role=role_value(input),hash=password_hash(new_password(input));
        Transaction transaction(database_); auto who=require(request,true,true);
        if (!database_.query("SELECT id FROM users WHERE username=?",{name}).empty()) throw Error(409,"username already exists");
        auto id=database_.insert("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",{name,hash,role});
        finish(transaction,"user_created",who.id,{{"user_id",id},{"role",role}});
        reply(response,{{"user",{{"id",id},{"username",name},{"role",role},{"disabled",false}}}},201);
    });
    server.Patch(R"(/api/users/(\d+))",[this](const auto& request,auto& response) {
        Transaction transaction(database_); auto who=require(request,true,true); auto id=route_id(request);
        auto input=input_body(request); fields(input,{"role","disabled"});
        if (input.empty()) throw Error(400,"empty update");
        auto rows=database_.query("SELECT id,username,role,disabled FROM users WHERE id=?",{id});
        if (rows.empty()) throw Error(404,"user not found");
        auto role=input.contains("role") ? role_value(input) : rows[0]["role"].template get<std::string>();
        bool disabled=rows[0]["disabled"]!=0;
        if (input.contains("disabled")) { if (!input["disabled"].is_boolean()) throw Error(400,"disabled must be a boolean"); disabled=input["disabled"].template get<bool>(); }
        protect_last_admin(rows[0],role,disabled);
        database_.query("UPDATE users SET role=?,disabled=? WHERE id=?",{role,disabled ? 1 : 0,id}); invalidate(id);
        finish(transaction,"user_updated",who.id,{{"user_id",id},{"role",role},{"disabled",disabled}});
        reply(response,{{"ok",true}});
    });
    server.Delete(R"(/api/users/(\d+))",[this](const auto& request,auto& response) {
        Transaction transaction(database_); auto who=require(request,true,true); auto id=route_id(request);
        auto rows=database_.query("SELECT id,role,disabled FROM users WHERE id=?",{id});
        if (rows.empty()) throw Error(404,"user not found");
        protect_last_admin(rows[0],"user",true);
        database_.query("DELETE FROM users WHERE id=?",{id});
        finish(transaction,"user_deleted",who.id,{{"user_id",id}}); reply(response,{{"ok",true}});
    });
    server.Post(R"(/api/users/(\d+)/password)",[this](const auto& request,auto& response) {
        require(request,true,true); auto id=route_id(request); auto input=input_body(request);
        fields(input,{"password"}); auto hash=password_hash(new_password(input));
        Transaction transaction(database_); auto who=require(request,true,true);
        if (database_.query("SELECT id FROM users WHERE id=?",{id}).empty()) throw Error(404,"user not found");
        database_.query("UPDATE users SET password_hash=? WHERE id=?",{hash,id}); invalidate(id);
        finish(transaction,"password_reset",who.id,{{"user_id",id}}); reply(response,{{"ok",true},{"login_required",true}});
    });
}
Json read_admin_input(bool from_stdin) {
    if (from_stdin) {
        std::string input; char c;
        while (std::cin.get(c)) { if (input.size()>=4096) throw Error(413,"initialization input too large"); input+=c; }
        try { return Json::parse(input); } catch (const Json::exception&) { throw Error(400,"invalid initialization JSON"); }
    }
    if (!isatty(STDIN_FILENO)) throw Error(400,"use --init-admin-stdin when input is not a terminal");
    std::cerr << "Administrator username: " << std::flush;
    std::string name;
    if (!std::getline(std::cin,name)) throw Error(400,"initialization cancelled");
    auto password=hidden_line("Password (12+ bytes): ");
    auto confirmation=hidden_line("Confirm password: ");
    if (!equal_secret(password,confirmation)) throw Error(400,"passwords do not match");
    return {{"username",name},{"password",password}};
}
}
