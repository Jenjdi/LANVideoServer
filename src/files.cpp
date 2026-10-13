#include "files.hpp"
#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <fcntl.h>
#include <limits>
#include <poll.h>
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>
extern char** environ;
namespace lv {
File::~File() { if (fd>=0) ::close(fd); }
struct stat File::stat() const {
    struct stat result{};
    if (::fstat(fd,&result)!=0) throw Error(404,"file unavailable");
    return result;
}
FilePtr directory(const fs::path& absolute) {
    if (!absolute.is_absolute()) throw Error(400,"directory must be absolute");
    auto current=std::make_shared<File>(::open("/",O_RDONLY|O_DIRECTORY|O_CLOEXEC));
    if (current->fd<0) throw Error(500,"cannot open filesystem root");
    // Walk every component without following symlinks, including root parents.
    for (const auto& component:absolute.relative_path()) {
        auto part=component.string();
        if (part.empty() || part=="." || part=="..") throw Error(400,"invalid directory path");
        int fd=::openat(current->fd,part.c_str(),O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
        if (fd<0) throw Error(errno==ENOENT ? 404 : 403,"shared directory unavailable or unsafe");
        current=std::make_shared<File>(fd);
    }
    return current;
}
FilePtr regular_file(const FilePtr& root,const fs::path& relative) {
    if (relative.empty() || relative.is_absolute()) throw Error(403,"unsafe media path");
    auto current=root;
    std::vector<std::string> components;
    for (const auto& part:relative) {
        auto value=part.string();
        if (value.empty() || value=="." || value==".." || value.starts_with('.') || value.find('\0')!=std::string::npos || value.find('\\')!=std::string::npos) throw Error(403,"unsafe media path");
        components.push_back(value);
    }
    for (size_t i=0;i<components.size();++i) {
        int flags=O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK;
        if (i+1<components.size()) flags|=O_DIRECTORY;
        int fd=::openat(current->fd,components[i].c_str(),flags);
        if (fd<0) throw Error(errno==ENOENT ? 404 : 403,"media file missing or unsafe");
        current=std::make_shared<File>(fd);
    }
    if (!S_ISREG(current->stat().st_mode)) throw Error(403,"media must be a regular file");
    return current;
}
FilePtr private_directory(const fs::path& absolute) {
    if (!absolute.is_absolute() || absolute == absolute.root_path()) throw Error(400,"private directory must not be filesystem root");
    auto current=directory("/");
    for (const auto& component:absolute.relative_path()) {
        auto part=component.string();
        if (part.empty() || part=="." || part==".." || part.find('\0')!=std::string::npos) throw Error(400,"invalid private directory path");
        if (::mkdirat(current->fd,part.c_str(),0700)!=0 && errno!=EEXIST) throw Error(500,"cannot create private directory");
        int fd=::openat(current->fd,part.c_str(),O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
        if (fd<0) throw Error(403,"private directory unavailable or unsafe");
        current=std::make_shared<File>(fd);
    }
    if (current->stat().st_uid!=::geteuid() || ::fchmod(current->fd,0700)!=0) throw Error(403,"private directory must be owned by the service account");
    return current;
}
long long modified_ns(const struct stat& stat) {
    if (stat.st_mtim.tv_nsec<0 || stat.st_mtim.tv_nsec>=1000000000LL || stat.st_mtim.tv_sec<0 ||
        stat.st_mtim.tv_sec>(std::numeric_limits<long long>::max()-stat.st_mtim.tv_nsec)/1000000000LL) throw Error(400,"unsupported file timestamp");
    return stat.st_mtim.tv_sec*1000000000LL+stat.st_mtim.tv_nsec;
}
std::string format_of(const std::string& name) {
    auto extension=fs::path(name).extension().string();
    if (!extension.empty()) extension.erase(0,1);
    std::transform(extension.begin(),extension.end(),extension.begin(),[](unsigned char c){ return static_cast<char>(std::tolower(c)); });
    return extension;
}
bool video_format(const std::string& format) {
    static const std::array<std::string,12> allowed={"mp4","m4v","webm","mkv","mov","avi","ogg","ogv","mpeg","mpg","ts","wmv"};
    return std::find(allowed.begin(),allowed.end(),format)!=allowed.end();
}
void upload_name(const std::string& name) {
    if (name.empty() || name.size()>200 || name.starts_with('.') || name.find_first_of("/\\:")!=std::string::npos)
        throw Error(400,"upload requires a simple filename of 1 to 200 bytes");
    for (unsigned char c:name) if (c<32 || c==127) throw Error(400,"invalid filename control character");
    try { (void)Json(name).dump(); } catch (const Json::exception&) { throw Error(400,"filename must be valid UTF-8"); }
    if (!video_format(format_of(name))) throw Error(415,"unsupported video file extension");
}
std::string encoded_filename(const std::string& name) {
    static constexpr char digits[]="0123456789ABCDEF";
    std::string encoded;
    for (unsigned char c:name) {
        if (std::isalnum(c) && c<128) encoded+=static_cast<char>(c);
        else { encoded+='%'; encoded+=digits[c>>4]; encoded+=digits[c&15]; }
    }
    return encoded;
}
bool container_signature(const FilePtr& file,const std::string& format) {
    std::array<unsigned char,16> b{};
    auto n=::pread(file->fd,b.data(),b.size(),0);
    if (n<4) return false;
    auto is=[&](size_t offset,const std::string& expected) { return n>=static_cast<ssize_t>(offset+expected.size()) && std::equal(expected.begin(),expected.end(),b.begin()+static_cast<ptrdiff_t>(offset)); };
    if (format=="mp4" || format=="m4v" || format=="mov") return is(4,"ftyp") || is(4,"moov") || is(4,"mdat") || is(4,"wide");
    if (format=="webm" || format=="mkv") return b[0]==0x1a && b[1]==0x45 && b[2]==0xdf && b[3]==0xa3;
    if (format=="avi") return is(0,"RIFF") && is(8,"AVI ");
    if (format=="ogg" || format=="ogv") return is(0,"OggS");
    if (format=="ts") return b[0]==0x47;
    if (format=="mpeg" || format=="mpg") return b[0]==0 && b[1]==0 && b[2]==1;
    if (format=="wmv") return b[0]==0x30 && b[1]==0x26 && b[2]==0xb2 && b[3]==0x75;
    return false;
}
Probe probe_video(const FilePtr& file,const std::string& executable) {
    Probe result;
    if (executable.empty() || ::access(executable.c_str(),X_OK)!=0) return result;
    int pipes[2];
    if (::pipe2(pipes,O_CLOEXEC|O_NONBLOCK)!=0) throw Error(503,"metadata pipe unavailable");
    File read_pipe(pipes[0]),write_pipe(pipes[1]);
    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_adddup2(&actions,file->fd,3);
    posix_spawn_file_actions_adddup2(&actions,write_pipe.fd,STDOUT_FILENO);
    posix_spawn_file_actions_addopen(&actions,STDERR_FILENO,"/dev/null",O_WRONLY,0);
    posix_spawn_file_actions_addclosefrom_np(&actions,4);
    std::vector<std::string> args={executable,"-v","error","-max_alloc","16777216","-probesize","1048576","-analyzeduration","1000000","-protocol_whitelist","file,pipe","-show_entries","format=duration:stream=codec_type","-of","json","-i","/proc/self/fd/3"};
    std::vector<char*> argv;
    for (auto& arg:args) argv.push_back(arg.data());
    argv.push_back(nullptr);
    pid_t child=0;
    int rc=posix_spawn(&child,executable.c_str(),&actions,nullptr,argv.data(),environ);
    posix_spawn_file_actions_destroy(&actions);
    if (rc!=0) return result;
    ::close(write_pipe.fd); write_pipe.fd=-1;
    result.state="invalid";
    auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(2);
    std::string output;
    int status=0;
    bool exited=false,overflow=false;
    while (std::chrono::steady_clock::now()<deadline) {
        char buffer[4096]; ssize_t n;
        while ((n=::read(read_pipe.fd,buffer,sizeof(buffer)))>0) {
            output.append(buffer,static_cast<size_t>(n));
            if (output.size()>16384) { overflow=true; break; }
        }
        if (overflow) break;
        if (::waitpid(child,&status,WNOHANG)==child) { exited=true; break; }
        pollfd poller{read_pipe.fd,POLLIN,0}; ::poll(&poller,1,20);
    }
    if (!exited) { ::kill(child,SIGKILL); while (::waitpid(child,&status,0)<0 && errno==EINTR) {} return result; }
    char buffer[4096]; ssize_t n;
    while ((n=::read(read_pipe.fd,buffer,sizeof(buffer)))>0 && output.size()<16384) output.append(buffer,static_cast<size_t>(n));
    if (!WIFEXITED(status) || WEXITSTATUS(status)!=0 || output.size()>16384) return result;
    try {
        auto parsed=Json::parse(output);
        bool video=false;
        for (const auto& stream:parsed.value("streams",Json::array())) if (stream.value("codec_type","")=="video") video=true;
        if (!video) return result;
        result.state="ok";
        if (parsed.contains("format") && parsed["format"].contains("duration")) {
            auto text=parsed["format"]["duration"].get<std::string>(); size_t consumed=0;
            double duration=std::stod(text,&consumed);
            if (consumed==text.size() && std::isfinite(duration) && duration>=0) result.duration=duration;
        }
    } catch (const std::exception&) { /* Basic information remains available. */ }
    return result;
}
}
