#pragma once
#include "core.hpp"
#include <memory>
#include <sys/stat.h>
namespace lv {
class File {
public:
    int fd;
    explicit File(int descriptor):fd(descriptor) {}
    ~File();
    File(const File&) = delete;
    File& operator=(const File&) = delete;
    struct stat stat() const;
};
using FilePtr = std::shared_ptr<File>;
FilePtr directory(const fs::path& absolute);
FilePtr private_directory(const fs::path& absolute);
FilePtr regular_file(const FilePtr& root, const fs::path& relative);
long long modified_ns(const struct stat& stat);
std::string format_of(const std::string& name);
bool video_format(const std::string& format);
void upload_name(const std::string& name);
std::string encoded_filename(const std::string& name);
struct Probe { Json duration=nullptr; std::string state="unavailable"; };
Probe probe_video(const FilePtr& file, const std::string& executable);
bool container_signature(const FilePtr& file,const std::string& format);
}
