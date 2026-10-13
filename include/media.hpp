#pragma once
#include "auth.hpp"
#include "files.hpp"
namespace lv {
class Media {
    friend class Uploads;
    friend class Shares;
    Database& database_;
    Auth& auth_;
    Config config_;
    std::mutex operations_;
    std::string root();
    Json lookup(long long id,const Identity& who);
    Json register_file(const std::string& root,const std::string& relative,const FilePtr& file,const Probe& probe);
    void finish(Transaction& transaction,const std::string& event,long long actor,const Json& detail);
    void serve(const httplib::Request& request,httplib::Response& response,bool invalid_range=false);
    void upload(const httplib::Request& request,httplib::Response& response,const httplib::ContentReader& reader);
public:
    Media(Database& database,Auth& auth,const Config& config);
    void routes(httplib::Server& server);
    void upload_preflight(const httplib::Request& request);
    void range_failure(const httplib::Request& request,httplib::Response& response);
    // Reuse the exact Range/path implementation for narrowly scoped sharing.
    // Authorization is repeated before each bounded write, including live revocation.
    void serve_authorized(const httplib::Request& request,httplib::Response& response,
                          std::function<Json()> authorize,bool invalid_range=false);
};
}
