#include "http_server.hpp"
#include "files.hpp"
#include <algorithm>
#include <array>
#include <arpa/inet.h>
#include <chrono>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

namespace lv {
namespace {
using Clock = std::chrono::steady_clock;
class RequestStream final : public httplib::Stream {
    int fd_;
    const std::atomic<socket_t>& listener_;
    Clock::time_point read_deadline_;
    std::array<char,4096> buffer_{};
    size_t offset_=0, length_=0, header_bytes_=0;
    std::string header_tail_;
    bool headers_done_=false,read_failed_=false;
    bool wait(short events,Clock::time_point deadline) const {
        while (listener_.load()!=INVALID_SOCKET) {
            auto remaining=std::chrono::duration_cast<std::chrono::milliseconds>(deadline-Clock::now()).count();
            if (remaining<=0) return false;
            pollfd item{fd_,events,0};
            int result=::poll(&item,1,static_cast<int>(std::min<decltype(remaining)>(remaining,100)));
            if (result<0 && errno==EINTR) continue;
            if (result<0 || (item.revents&(POLLERR|POLLNVAL))) return false;
            if (result>0) return (item.revents&(events|POLLHUP))!=0;
        }
        return false;
    }
    void address(bool peer,std::string& ip,int& port) const {
        sockaddr_in value{}; socklen_t length=sizeof(value);
        int result=peer ? ::getpeername(fd_,reinterpret_cast<sockaddr*>(&value),&length)
                        : ::getsockname(fd_,reinterpret_cast<sockaddr*>(&value),&length);
        std::array<char,INET_ADDRSTRLEN> text{};
        if (result==0 && ::inet_ntop(AF_INET,&value.sin_addr,text.data(),text.size())) {
            ip=text.data(); port=ntohs(value.sin_port);
        }
    }
public:
    RequestStream(int fd,const std::atomic<socket_t>& listener):fd_(fd),listener_(listener) { begin_request(); }
    void begin_request() {
        read_deadline_=Clock::now()+std::chrono::seconds(10);
        header_bytes_=0; header_tail_.clear(); headers_done_=false; read_failed_=false;
    }
    void allow_upload() { read_deadline_=Clock::now()+std::chrono::seconds(120); }
    bool expired() const { return read_failed_ || Clock::now()>=read_deadline_ || header_bytes_>16384; }
    bool idle(time_t seconds) const { return offset_<length_ || wait(POLLIN,Clock::now()+std::chrono::seconds(seconds)); }
    bool is_readable() const override {
        return Clock::now()<read_deadline_ && (offset_<length_ || wait(POLLIN,read_deadline_));
    }
    bool is_writable() const override { return wait(POLLOUT,Clock::now()+std::chrono::seconds(10)); }
    ssize_t read(char* target,size_t size) override {
        if (!size) return 0;
        if (!is_readable()) { read_failed_=true; return -1; }
        if (offset_==length_) {
            ssize_t received;
            do { received=::recv(fd_,buffer_.data(),buffer_.size(),MSG_DONTWAIT); } while (received<0 && errno==EINTR);
            if (received<=0) return received;
            offset_=0; length_=static_cast<size_t>(received);
        }
        auto count=std::min(size,length_-offset_);
        for (size_t i=0;i<count && !headers_done_;++i) {
            if (++header_bytes_>16384) return -1;
            header_tail_+=buffer_[offset_+i];
            if (header_tail_.size()>4) header_tail_.erase(0,1);
            if (header_tail_=="\r\n\r\n" || header_tail_.ends_with("\n\n")) {
                headers_done_=true;
                read_deadline_=Clock::now()+std::chrono::seconds(10);
            }
        }
        std::copy_n(buffer_.data()+offset_,count,target); offset_+=count;
        return static_cast<ssize_t>(count);
    }
    ssize_t write(const char* bytes,size_t size) override {
        if (!is_writable()) return -1;
        ssize_t sent;
        do { sent=::send(fd_,bytes,size,MSG_NOSIGNAL|MSG_DONTWAIT); } while (sent<0 && errno==EINTR);
        return sent;
    }
    void get_remote_ip_and_port(std::string& ip,int& port) const override { address(true,ip,port); }
    void get_local_ip_and_port(std::string& ip,int& port) const override { address(false,ip,port); }
    socket_t socket() const override { return fd_; }
};
thread_local RequestStream* current_stream=nullptr;
}
void HttpServer::allow_upload_body() { if (current_stream) current_stream->allow_upload(); }
bool HttpServer::process_and_close_socket(socket_t socket) {
    File connection(socket);
    RequestStream stream(socket,svr_sock_);
    current_stream=&stream;
    struct Reset { ~Reset() { current_stream=nullptr; } } reset;
    std::string remote,local; int remote_port=0,local_port=0;
    stream.get_remote_ip_and_port(remote,remote_port); stream.get_local_ip_and_port(local,local_port);
    bool result=false;
    for (size_t count=0;count<keep_alive_max_count_ && stream.idle(keep_alive_timeout_sec_);++count) {
        stream.begin_request();
        bool closed=false;
        result=process_request(stream,remote,remote_port,local,local_port,count+1==keep_alive_max_count_,closed,nullptr);
        if (!result || closed || stream.expired()) break;
    }
    ::shutdown(socket,SHUT_RDWR);
    return result;
}
}
