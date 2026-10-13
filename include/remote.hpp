#pragma once
#include "auth.hpp"
#include <memory>
namespace lv {
// Local and HTTPS ingress adapters; BT outgoing proxy is configured separately.
class AccessAdapter {
public:
    virtual ~AccessAdapter() = default;
    virtual void guard(const httplib::Request& request) const = 0;
    virtual Json test(const std::string& instance) const = 0;
};
class Remote {
    Auth& auth_;
    Config config_;
    std::unique_ptr<AccessAdapter> adapter_;
    std::string instance_;
    std::mutex mutex_;
    Json last_={{"state","not_tested"},{"reachable",false}};
    long long tested_=0;
public:
    Remote(Auth& auth,const Config& config);
    void guard(const httplib::Request& request) const;
    void routes(httplib::Server& server);
};
// Common cryptographic helpers. Raw secrets must never be logged.
std::string random_secret();
std::string secret_hash(const std::string& value);
bool valid_secret(const std::string& value);
}
