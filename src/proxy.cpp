#include "proxy.hpp"
#include "files.hpp"
#include <sodium.h>
#include <arpa/inet.h>
#include <chrono>
#include <poll.h>
#include <sys/socket.h>
namespace lv {
namespace {
int port(const Json& v) {
    if (!v.is_number_integer() || v.get<long long>()<1 || v.get<long long>()>65535) throw Error(400,"port must be 1..65535");
    return v.get<int>();
}
in_addr ipv4(const Json& v) {
    in_addr address{};
    if (!v.is_string() || v.get<std::string>().find('\0')!=std::string::npos || inet_pton(AF_INET,v.get<std::string>().c_str(),&address)!=1) throw Error(400,"this version requires an IPv4 literal for proxy/test host");
    return address;
}
class Wire {
    FilePtr socket_;
    std::chrono::steady_clock::time_point deadline_;
    void wait(short events) {
        pollfd fd{socket_->fd,events,0}; int rc;
        do {
            auto ms=std::chrono::duration_cast<std::chrono::milliseconds>(deadline_-std::chrono::steady_clock::now()).count();
            if (ms<=0) throw Error(408,"proxy connection test timed out");
            rc=::poll(&fd,1,static_cast<int>(ms));
        } while (rc<0 && errno==EINTR);
        if (rc==0) throw Error(408,"proxy connection test timed out");
        if (rc<0 || (fd.revents&(POLLERR|POLLNVAL))) throw Error(502,"proxy connection failed");
    }
public:
    explicit Wire(const Json& proxy):deadline_(std::chrono::steady_clock::now()+std::chrono::milliseconds(proxy["timeout_ms"].get<int>())) {
        int fd=::socket(AF_INET,SOCK_STREAM|SOCK_NONBLOCK|SOCK_CLOEXEC,0);
        if (fd<0) throw Error(502,"cannot create proxy test connection");
        socket_=std::make_shared<File>(fd);
        sockaddr_in a{}; a.sin_family=AF_INET; a.sin_addr=ipv4(proxy["host"]); a.sin_port=htons(static_cast<uint16_t>(port(proxy["port"])));
        if (::connect(fd,reinterpret_cast<sockaddr*>(&a),sizeof(a))<0 && errno!=EINPROGRESS) throw Error(502,"cannot connect to proxy");
        wait(POLLOUT); int failure=0; socklen_t length=sizeof(failure);
        if (::getsockopt(fd,SOL_SOCKET,SO_ERROR,&failure,&length)!=0 || failure) throw Error(502,"proxy refused TCP connection");
    }
    void send(const std::string& bytes) {
        size_t done=0; while (done<bytes.size()) {
            wait(POLLOUT); auto n=::send(socket_->fd,bytes.data()+done,bytes.size()-done,MSG_NOSIGNAL);
            if (n<0 && (errno==EINTR || errno==EAGAIN)) continue;
            if (n<=0) throw Error(502,"proxy connection closed while sending");
            done+=static_cast<size_t>(n);
        }
    }
    std::string read(size_t count) {
        std::string result(count,'\0'); size_t done=0;
        while (done<count) {
            wait(POLLIN); auto n=::recv(socket_->fd,result.data()+done,count-done,0);
            if (n<0 && (errno==EINTR || errno==EAGAIN)) continue;
            if (n<=0) throw Error(502,"proxy connection closed during handshake");
            done+=static_cast<size_t>(n);
        }
        return result;
    }
};
}
Json validate_proxy(const Json& input) {
    if (!input.is_object()) throw Error(400,"proxy object required");
    for (auto it=input.begin();it!=input.end();++it)
        if (it.key()!="type" && it.key()!="host" && it.key()!="port" && it.key()!="username" && it.key()!="password" && it.key()!="timeout_ms") throw Error(400,"unknown proxy setting");
    auto type=input.value("type",std::string("none"));
    if (type!="none" && type!="socks5" && type!="http") throw Error(400,"proxy type must be none, socks5 or http");
    int timeout=3000;
    if (input.contains("timeout_ms")) {
        if (!input["timeout_ms"].is_number_integer() || input["timeout_ms"].get<long long>()<100 || input["timeout_ms"].get<long long>()>10000) throw Error(400,"proxy timeout must be 100..10000 ms");
        timeout=input["timeout_ms"].get<int>();
    }
    Json result={{"type",type},{"host",""},{"port",0},{"username",""},{"password",""},{"timeout_ms",timeout}};
    if (type=="none") return result;
    if (!input.contains("host") || !input.contains("port")) throw Error(400,"proxy host and port required");
    (void)ipv4(input["host"]); result["host"]=input["host"]; result["port"]=port(input["port"]);
    for (const auto* key:{"username","password"}) {
        auto text=input.value(key,std::string(""));
        if (text.size()>255 || text.find_first_of("\r\n")!=std::string::npos || text.find('\0')!=std::string::npos) throw Error(400,"invalid proxy credentials");
        result[key]=text;
    }
    if (result["username"].get<std::string>().empty()!=result["password"].get<std::string>().empty()) throw Error(400,"proxy username and password must both be set or both empty");
    if (result["username"].get<std::string>().find(':')!=std::string::npos) throw Error(400,"proxy username cannot contain colon");
    return result;
}
Json proxy_summary(const Json& p) {
    return {{"type",p["type"]},{"host",p["host"]},{"port",p["port"]},{"timeout_ms",p["timeout_ms"]},{"credentials_configured",!p["password"].get<std::string>().empty()}};
}
Json test_proxy(const Json& p,const Json& target) {
    if (p["type"]=="none") throw Error(400,"configure a proxy before testing");
    if (!target.is_object() || target.size()!=2 || !target.contains("host") || !target.contains("port")) throw Error(400,"test target requires IPv4 host and port");
    auto a=ipv4(target["host"]); auto destination=port(target["port"]);
    auto began=std::chrono::steady_clock::now(); Wire wire(p);
    auto username=p["username"].get<std::string>(),password=p["password"].get<std::string>();
    if (p["type"]=="socks5") {
        wire.send(username.empty()?std::string("\x05\x01\x00",3):std::string("\x05\x01\x02",3));
        auto method=wire.read(2);
        if (method[0]!=5 || method[1]!=(username.empty()?0:2)) throw Error(502,"SOCKS5 authentication method rejected");
        if (method[1]==2 && !username.empty()) {
            std::string auth(1,'\x01'); auth+=static_cast<char>(username.size()); auth+=username; auth+=static_cast<char>(password.size()); auth+=password;
            wire.send(auth); auto result=wire.read(2); if (result[0]!=1 || result[1]!=0) throw Error(502,"SOCKS5 credentials rejected");
        } else if (method[1]!=0) throw Error(502,"unexpected SOCKS5 authentication method");
        std::string connect("\x05\x01\x00\x01",4); connect.append(reinterpret_cast<const char*>(&a.s_addr),4);
        connect+=static_cast<char>(destination>>8); connect+=static_cast<char>(destination&255); wire.send(connect);
        auto result=wire.read(4);
        if (result[0]!=5 || result[1]!=0 || result[2]!=0) throw Error(502,"SOCKS5 target connection rejected");
        auto kind=static_cast<unsigned char>(result[3]);
        if (kind==1) (void)wire.read(6); else if (kind==4) (void)wire.read(18); else if (kind==3) { auto n=static_cast<unsigned char>(wire.read(1)[0]); (void)wire.read(n+2); } else throw Error(502,"invalid SOCKS5 reply");
    } else {
        auto host=target["host"].get<std::string>()+":"+std::to_string(destination);
        std::string request="CONNECT "+host+" HTTP/1.1\r\nHost: "+host+"\r\n";
        if (!username.empty()) {
            auto text=username+":"+password; std::string encoded(sodium_base64_ENCODED_LEN(text.size(),sodium_base64_VARIANT_ORIGINAL),'\0');
            sodium_bin2base64(encoded.data(),encoded.size(),reinterpret_cast<const unsigned char*>(text.data()),text.size(),sodium_base64_VARIANT_ORIGINAL); encoded.resize(encoded.find('\0'));
            request+="Proxy-Authorization: Basic "+encoded+"\r\n";
        }
        wire.send(request+"\r\n"); std::string response;
        while (!response.ends_with("\r\n\r\n") && response.size()<8192) response+=wire.read(1);
        if (!response.ends_with("\r\n\r\n")) throw Error(502,"HTTP proxy response too large");
        if (response.starts_with("HTTP/1.1 407 ") || response.starts_with("HTTP/1.0 407 ")) throw Error(502,"HTTP proxy credentials rejected");
        if (!response.starts_with("HTTP/1.1 200 ") && !response.starts_with("HTTP/1.0 200 ")) throw Error(502,"HTTP CONNECT target connection rejected");
    }
    auto elapsed=std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now()-began).count();
    return {{"connected",true},{"elapsed_ms",elapsed},{"scope","proxy handshake and target TCP connection only"}};
}
}
