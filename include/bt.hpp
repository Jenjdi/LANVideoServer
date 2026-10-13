#pragma once
#include "auth.hpp"
#include "files.hpp"
#include <libtorrent/session.hpp>
#include <libtorrent/torrent_info.hpp>
#include <map>
#include <set>
#include <thread>
#include <array>
#include <atomic>
#include <chrono>
namespace lv {
class Bt {
    Database& db_;
    Auth& auth_;
    Config config_;
    std::mutex mutex_;
    std::unique_ptr<libtorrent::session> session_;
    std::map<long long,libtorrent::torrent_handle> handles_;
    std::set<long long> saving_;
    std::map<long long,Json> faults_;
    struct PieceRead {
        libtorrent::torrent_handle handle;
        std::shared_ptr<const std::string> bytes;
        std::chrono::steady_clock::time_point created;
        bool ready=false;
        int error=0;
    };
    struct Focus {
        libtorrent::torrent_handle handle;
        std::vector<int> pieces;
        std::chrono::steady_clock::time_point expires;
        bool elevated=false;
    };
    void release_focus(const Focus& focus);
    std::map<std::pair<long long,int>,std::shared_ptr<PieceRead>> reads_;
    std::map<long long,Focus> focus_;
    std::map<long long,FilePtr> directories_;
    std::set<std::pair<long long,int>> quarantined_;
    std::atomic<bool> stopping_{false};
    std::array<unsigned char,32> key_{};
    Json proxy_;
    std::jthread worker_;
    std::string root();
    std::shared_ptr<libtorrent::torrent_info> validate(const std::string& bytes);
    void add(const Json& row,const std::shared_ptr<libtorrent::torrent_info>& info);
    Json owned(const httplib::Request& request,bool mutation);
    Json status(const Json& row);
    void drain();
    void save(long long id);
    void audit(const std::string& event,long long actor,long long task=0);
    std::string encrypt(const Json& input);
    Json decrypt(const std::string& input);
    void read_alert(const libtorrent::alert* alert,long long id);
    void schedule();
    void focus(long long id,const libtorrent::torrent_handle& handle,int piece);
    Json schedule_status(long long id);
    Json file_list(const httplib::Request& request);
    void streaming_routes(httplib::Server& server);
    void serve_stream(const httplib::Request& request,httplib::Response& response,bool invalid_range=false);
    void validate_stream_path(const Json& row,const libtorrent::torrent_info& info,int piece);
    std::shared_ptr<const std::string> read_verified(const httplib::Request& request,long long id,const libtorrent::torrent_handle& handle,int piece,std::chrono::steady_clock::time_point deadline);
    void forget_stream(long long id);
public:
    Bt(Database& db,Auth& auth,const Config& config);
    ~Bt();
    void routes(httplib::Server& server);
    void preflight(const httplib::Request& request);
    void range_failure(const httplib::Request& request,httplib::Response& response);
    void stop_streams() { stopping_=true; }
};
}
