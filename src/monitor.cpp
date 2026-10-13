#include "monitor.hpp"
#include <charconv>
#include <limits>
#include <sys/statvfs.h>
namespace lv {
namespace {
long long parameter(const httplib::Request& request,const char* key,long long fallback,long long maximum) {
    auto count=request.get_param_value_count(key);
    if (count==0) return fallback;
    if (count!=1) throw Error(400,"duplicate monitor parameter");
    auto text=request.get_param_value(key); long long value=0;
    auto converted=std::from_chars(text.data(),text.data()+text.size(),value);
    if (converted.ec!=std::errc{} || converted.ptr!=text.data()+text.size() || value<1 || value>maximum) throw Error(400,"invalid monitor parameter");
    return value;
}
}
void Monitor::routes(httplib::Server& server) {
    server.Get("/api/admin/status",[this](const httplib::Request& request,httplib::Response& response) {
        Transaction snapshot(database_); auth_.require(request,true);
        if (!request.params.empty()) throw Error(400,"status takes no query parameters");
        auto at=now();
        auto count=[&](const std::string& sql,const Json& values=Json::array()) {return database_.query(sql,values)[0]["n"];};
        auto result=Json({{"version",LANVIDEO_VERSION},{"timestamp",at},
            {"uptime_seconds",std::chrono::duration_cast<std::chrono::seconds>(std::chrono::steady_clock::now()-started_).count()},
            {"mode",config_.remote_mode},{"users",{{"total",count("SELECT count(*) AS n FROM users")},{"enabled",count("SELECT count(*) AS n FROM users WHERE disabled=0")}}},
            {"media",{{"count",count("SELECT count(*) AS n FROM media")},{"catalog_bytes",count("SELECT coalesce(sum(size),0) AS n FROM media")}}},
            {"tasks",{{"total",count("SELECT count(*) AS n FROM tasks")},{"requested_paused",count("SELECT count(*) AS n FROM tasks WHERE paused=1")}}},
            {"sessions",count("SELECT count(*) AS n FROM sessions WHERE expires>?",{at})},
            {"shares",{{"active",count("SELECT count(*) AS n FROM shares WHERE revoked=0 AND expires>?",{at})},{"revoked",count("SELECT count(*) AS n FROM shares WHERE revoked=1")},{"play_sessions",count("SELECT count(*) AS n FROM share_sessions WHERE expires>?",{at})}}},
            {"audit_entries",count("SELECT count(*) AS n FROM audit")},
            {"limits",{{"worker_threads",8},{"queue_capacity",64},{"upload_bytes",config_.upload_limit}}}});
        snapshot.commit();
        struct statvfs filesystem{};
        result["data_fs_free_bytes"]=::statvfs(config_.data.c_str(),&filesystem)==0 ? Json(static_cast<unsigned long long>(filesystem.f_bavail)*filesystem.f_frsize) : Json(nullptr);
        reply(response,result);
    });
    server.Get("/api/admin/audit",[this](const httplib::Request& request,httplib::Response& response) {
        Transaction snapshot(database_); auth_.require(request,true);
        for (const auto& param:request.params) if (param.first!="limit" && param.first!="before_id") throw Error(400,"unknown audit parameter");
        auto limit=parameter(request,"limit",50,100),before=parameter(request,"before_id",std::numeric_limits<long long>::max(),std::numeric_limits<long long>::max());
        auto rows=database_.query("SELECT id,timestamp,actor,event,detail FROM audit WHERE id<? ORDER BY id DESC LIMIT ?",{before,limit});
        for (auto& row:rows) {
            auto detail=Json::parse(row["detail"].get<std::string>(),nullptr,false);
            row["detail"]=detail.is_object() ? detail : Json::object();
        }
        auto next=rows.size()==static_cast<size_t>(limit) ? rows.back()["id"] : Json(nullptr);
        snapshot.commit(); reply(response,{{"audit",rows},{"next_before_id",next}});
    });
}
}
