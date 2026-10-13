#include "remote.hpp"
#include "files.hpp"
#include <sodium.h>
#include <array>
#include <arpa/inet.h>
#include <fcntl.h>
#include <unistd.h>
#include <condition_variable>
#include <thread>
namespace lv {
std::string random_secret() {
    std::array<unsigned char,32> bytes{}; randombytes_buf(bytes.data(),bytes.size());
    std::array<char,65> hex{}; sodium_bin2hex(hex.data(),hex.size(),bytes.data(),bytes.size()); return hex.data();
}
bool valid_secret(const std::string& value) {
    return value.size()==64 && value.find_first_not_of("0123456789abcdef")==std::string::npos;
}
std::string secret_hash(const std::string& value) {
    std::array<unsigned char,32> hash{};
    crypto_hash_sha256(hash.data(),reinterpret_cast<const unsigned char*>(value.data()),value.size());
    std::array<char,65> hex{}; sodium_bin2hex(hex.data(),hex.size(),hash.data(),hash.size()); return hex.data();
}
namespace {
class LocalAccess final:public AccessAdapter {
public:
    void guard(const httplib::Request&) const override {}
    Json test(const std::string&) const override { return {{"state","local_only"},{"reachable",false},{"message","Local HTTP mode; no remote entry configured"}}; }
};
std::string ingress_key(const Config& config) {
    auto folder=directory(config.data);
    int fd=::openat(folder->fd,"ingress.key",O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
    if (fd>=0) {
        File file(fd); auto value=random_secret();
        if (::write(fd,value.data(),value.size())!=static_cast<ssize_t>(value.size()) || ::fsync(fd)!=0)
            throw Error(500,"cannot initialize ingress key");
    } else if (errno!=EEXIST) throw Error(500,"cannot initialize ingress key");
    auto file=regular_file(folder,"ingress.key"); auto stat=file->stat();
    if (stat.st_uid!=::geteuid() || (stat.st_mode&0777)!=0600 || stat.st_nlink!=1 || stat.st_size!=64)
        throw Error(500,"ingress key must be a private 0600 regular file with 64 hex characters");
    std::string value(64,'\0');
    if (::pread(file->fd,value.data(),64,0)!=64 || !valid_secret(value)) throw Error(500,"invalid ingress key");
    return value;
}
class ReverseProxyAccess final:public AccessAdapter {
    Config config_;
    std::string key_;
public:
    explicit ReverseProxyAccess(const Config& config):config_(config),key_(ingress_key(config)) {}
    void guard(const httplib::Request& request) const override {
        // A public health result is harmless and permits private readiness checks.
        if (request.method=="GET" && request.path=="/api/health" && !request.has_header("X-Lanvideo-Ingress") && !request.has_header("X-Forwarded-Proto")) return;
        auto value=request.get_header_value("X-Lanvideo-Ingress");
        if (request.remote_addr!="127.0.0.1" || request.get_header_value_count("X-Lanvideo-Ingress")!=1 || value.size()!=key_.size() || sodium_memcmp(value.data(),key_.data(),key_.size())!=0)
            throw Error(403,"HTTPS reverse proxy entry required");
        if (request.get_header_value_count("X-Forwarded-Proto")!=1 || request.get_header_value("X-Forwarded-Proto")!="https" || request.get_header_value_count("X-Forwarded-Host")!=1 || request.get_header_value("X-Forwarded-Host")!=config_.public_origin.substr(8))
            throw Error(403,"invalid HTTPS ingress metadata");
        auto ip=request.get_header_value("X-Real-IP"); std::array<unsigned char,16> address{};
        if (request.get_header_value_count("X-Real-IP")!=1 || ip.find('\0')!=std::string::npos || (inet_pton(AF_INET,ip.c_str(),address.data())!=1 && inet_pton(AF_INET6,ip.c_str(),address.data())!=1)) throw Error(400,"invalid proxy client address");
    }
    Json test(const std::string& instance) const override {
        auto authority=config_.public_origin.substr(8); auto colon=authority.find(':');
        httplib::SSLClient client(authority.substr(0,colon),colon==std::string::npos ? 443 : std::stoi(authority.substr(colon+1)));
        client.enable_server_certificate_verification(true);
        if (!config_.remote_ca.empty()) client.set_ca_cert_path(config_.remote_ca);
        client.set_connection_timeout(2,0); client.set_read_timeout(2,0); client.set_write_timeout(2,0);
        client.set_follow_location(false);
        std::mutex deadline_mutex; std::condition_variable_any deadline_wait;
        std::jthread deadline([&](std::stop_token stop) {
            std::unique_lock lock(deadline_mutex);
            deadline_wait.wait_for(lock,stop,std::chrono::seconds(6),[] { return false; });
            if (!stop.stop_requested()) client.stop();
        });
        auto nonce=random_secret(); std::string content;
        auto result=client.Get("/api/remote/probe?nonce="+nonce,[&](const char* bytes,size_t length) {
            if (length>4096-content.size()) return false;
            content.append(bytes,length); return true;
        });
        deadline.request_stop(); deadline.join();
        if (!result) {
            auto tls=result.error()==httplib::Error::SSLServerVerification || result.error()==httplib::Error::SSLServerHostnameVerification || result.error()==httplib::Error::SSLConnection;
            return {{"state",tls ? "tls_error" : "connection_error"},{"reachable",false},{"message",tls ? "TLS trust, hostname or handshake failed" : "HTTPS entry unreachable or timed out"}};
        }
        auto input=Json::parse(content,nullptr,false);
        if (result->status!=200 || !input.is_object() || input.value("nonce",Json())!=nonce || input.value("instance",Json())!=instance)
            return {{"state","entry_mismatch"},{"reachable",false},{"message","HTTPS entry did not reach this server"}};
        return {{"state","reachable"},{"reachable",true},{"message","Verified TLS entry and this server identity; local probe is not a cross-NAT test"}};
    }
};
}
Remote::Remote(Auth& auth,const Config& config):auth_(auth),config_(config),instance_(random_secret()) {
    if (config.remote_mode=="reverse_proxy") adapter_=std::make_unique<ReverseProxyAccess>(config);
    else adapter_=std::make_unique<LocalAccess>();
}
void Remote::guard(const httplib::Request& request) const { adapter_->guard(request); }
void Remote::routes(httplib::Server& server) {
    server.Get("/api/remote",[this](const auto& request,auto& response) {
        auth_.require(request,true); std::lock_guard lock(mutex_);
        reply(response,{{"adapter",config_.remote_mode},{"public_origin",config_.public_origin},{"last_test",last_},{"tested_at",tested_},{"cross_nat_verified",false},{"fallback","If the HTTPS entry fails, keep remote requests closed. An administrator can explicitly restart in local mode on a trusted LAN; no automatic HTTP downgrade."}});
    });
    server.Get("/api/remote/probe",[this](const auto& request,auto& response) {
        // Global ingress guard has already verified the private proxy boundary.
        if (config_.remote_mode!="reverse_proxy" || request.params.size()!=1 || request.get_param_value_count("nonce")!=1 || !valid_secret(request.get_param_value("nonce"))) throw Error(400,"invalid entry probe");
        reply(response,{{"nonce",request.get_param_value("nonce")},{"instance",instance_}});
    });
    server.Post("/api/remote/test",[this](const auto& request,auto& response) {
        auto who=auth_.require(request,true,true);
        if (!body(request).empty()) throw Error(400,"entry test takes no target input");
        std::unique_lock lock(mutex_,std::try_to_lock);
        if (!lock.owns_lock() || now()-tested_<5) throw Error(429,"entry test busy; retry after five seconds");
        tested_=now(); last_=adapter_->test(instance_);
        who=auth_.require(request,true,true);
        log("remote_entry_test",{{"actor",who.id},{"adapter",config_.remote_mode},{"state",last_["state"]}});
        reply(response,last_);
    });
}
}
