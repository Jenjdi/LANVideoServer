#pragma once
#include "media.hpp"
namespace lv {
// Durable, sequential chunks. Database offset acknowledges fsync'ed, SHA256-verified bytes.
class Uploads {
    Database& db_;
    Auth& auth_;
    Media& media_;
    Config config_;
    std::mutex mutex_;
    Json owned(const httplib::Request& request,bool mutation);
    FilePtr folder(const Json& row);
    FilePtr staged(const Json& row,const FilePtr& directory);
    Json status(const Json& row);
    void expire();
    void discard_staged(const Json& row);
public:
    static constexpr size_t chunk_size=1024*1024;
    Uploads(Database& db,Auth& auth,Media& media,const Config& config);
    void preflight(const httplib::Request& request);
    void routes(httplib::Server& server);
};
}
