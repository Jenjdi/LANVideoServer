#pragma once
#include "auth.hpp"
#include <chrono>
namespace lv {
class Monitor {
    Database& database_;
    Auth& auth_;
    Config config_;
    std::chrono::steady_clock::time_point started_=std::chrono::steady_clock::now();
public:
    Monitor(Database& database,Auth& auth,const Config& config):database_(database),auth_(auth),config_(config) {}
    void routes(httplib::Server& server);
};
}
