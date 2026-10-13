#pragma once
#include <httplib.h>
#include <nlohmann/json.hpp>
#include <sqlite3.h>
#include <filesystem>
#include <mutex>
#include <stdexcept>
#include <string>
namespace lv {
using Json = nlohmann::json;
namespace fs = std::filesystem;
struct Error : std::runtime_error {
    int status;
    Error(int code, const std::string& message): std::runtime_error(message), status(code) {}
};
struct Config {
    std::string bind, public_origin, log_level;
    int port;
    int session_ttl;
    size_t upload_limit;
    std::string ffprobe;
    std::string remote_mode, remote_ca;
    fs::path data, web;
    static Config load();
};
void log(const std::string& event, const Json& fields = Json::object(), const std::string& level = "info");
void reply(httplib::Response& response, const Json& body, int status = 200);
Json body(const httplib::Request& request, size_t limit = 4096);
void validate_origin(const httplib::Request& request, const Config& config);
void reject_duplicate_query(const httplib::Request& request);
long long now();
class Database {
    sqlite3* handle_ = nullptr;
    std::recursive_mutex mutex_;
public:
    explicit Database(const fs::path& file);
    ~Database();
    Database(const Database&) = delete;
    Database& operator=(const Database&) = delete;
    Json query(const std::string& sql, const Json& parameters = Json::array());
    long long insert(const std::string& sql, const Json& parameters);
    std::recursive_mutex& mutex() { return mutex_; }
};
// Holds the connection lock for the entire transaction; rollback on exceptions.
class Transaction {
    Database& database_;
    std::unique_lock<std::recursive_mutex> lock_;
    bool committed_ = false;
public:
    explicit Transaction(Database& database);
    ~Transaction();
    void commit();
    Transaction(const Transaction&) = delete;
    Transaction& operator=(const Transaction&) = delete;
};
}
