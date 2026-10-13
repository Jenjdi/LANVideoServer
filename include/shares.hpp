#pragma once
#include "media.hpp"
namespace lv {
class Shares {
    Database& database_;
    Auth& auth_;
    Media& media_;
    Config config_;
    Json resolve(const std::string& session_hash);
    std::string session(const httplib::Request& request) const;
    std::string cookie(const std::string& value,long long ttl) const;
    void serve(const httplib::Request& request,httplib::Response& response,bool invalid_range=false);
public:
    Shares(Database& database,Auth& auth,Media& media,const Config& config);
    void routes(httplib::Server& server);
    void range_failure(const httplib::Request& request,httplib::Response& response);
};
}
