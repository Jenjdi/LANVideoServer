#pragma once
#include "core.hpp"
#include <semaphore>
#include <unordered_map>
namespace lv {
struct Identity {
    long long id;
    std::string username, role, token_hash, csrf;
    bool cookie;
    Json public_user() const;
};
class Auth {
    Database& database_;
    Config config_;
    std::string dummy_hash_;
    std::counting_semaphore<2> hash_slots_{2};
    struct Window { long long start; int count; };
    std::mutex rate_mutex_;
    std::unordered_map<std::string, Window> login_windows_;
    void throttle(const std::string& address);
    std::string password_hash(const std::string& password);
    bool verify(const std::string& encoded, const std::string& password);
    void audit(const std::string& event, const Json& actor, const Json& detail);
    void finish(Transaction& transaction, const std::string& event, const Json& actor, const Json& detail);
    void origin(const httplib::Request& request) const;
    std::string cookie_header(const std::string& token, int lifetime) const;
    void invalidate(long long id);
    void protect_last_admin(const Json& old_user, const std::string& role, bool disabled);
public:
    Auth(Database& database, const Config& config);
    bool initialized();
    void initialize(const Json& input);
    Identity require(const httplib::Request& request, bool admin = false, bool mutation = false);
    void routes(httplib::Server& server);
};
// Secrets never enter process arguments, environment variables or logs.
Json read_admin_input(bool from_stdin);
}
