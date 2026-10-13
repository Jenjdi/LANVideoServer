#include "uploads.hpp"
#include <sodium.h>
#include <array>
#include <charconv>
#include <fcntl.h>
#include <sys/syscall.h>
#include <unistd.h>
namespace lv {
namespace {
std::string sha(const std::string& bytes) {
    std::array<unsigned char,crypto_hash_sha256_BYTES> hash{};
    crypto_hash_sha256(hash.data(),reinterpret_cast<const unsigned char*>(bytes.data()),bytes.size());
    std::array<char,65> hex{}; sodium_bin2hex(hex.data(),hex.size(),hash.data(),hash.size()); return hex.data();
}
bool hexhash(const Json& value) {
    if (!value.is_string()) return false;
    auto text=value.get<std::string>(); return text.size()==64 && text.find_first_not_of("0123456789abcdef")==std::string::npos;
}
std::string full_hash(const FilePtr& file,size_t length) {
    crypto_hash_sha256_state state; crypto_hash_sha256_init(&state);
    std::array<unsigned char,65536> bytes{};
    for (size_t offset=0;offset<length;) {
        auto n=::pread(file->fd,bytes.data(),std::min(bytes.size(),length-offset),static_cast<off_t>(offset));
        if (n<=0) throw Error(507,"cannot read staged upload");
        crypto_hash_sha256_update(&state,bytes.data(),static_cast<unsigned long long>(n)); offset+=static_cast<size_t>(n);
    }
    std::array<unsigned char,32> hash{}; crypto_hash_sha256_final(&state,hash.data());
    std::array<char,65> hex{}; sodium_bin2hex(hex.data(),hex.size(),hash.data(),hash.size()); return hex.data();
}
size_t number_header(const httplib::Request& request,const char* name) {
    if (request.get_header_value_count(name)!=1) throw Error(400,"one offset and length header required");
    auto text=request.get_header_value(name); size_t value=0;
    auto parsed=std::from_chars(text.data(),text.data()+text.size(),value);
    if (text.empty() || parsed.ec!=std::errc{} || parsed.ptr!=text.data()+text.size()) throw Error(400,"invalid offset or length");
    return value;
}
}
Uploads::Uploads(Database& db,Auth& auth,Media& media,const Config& config):db_(db),auth_(auth),media_(media),config_(config) {
    db_.query("CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,owner INTEGER NOT NULL,root TEXT NOT NULL,name TEXT NOT NULL,size INTEGER NOT NULL,hashes TEXT NOT NULL,fingerprint TEXT NOT NULL,offset INTEGER NOT NULL DEFAULT 0,expires INTEGER NOT NULL,device INTEGER NOT NULL,inode INTEGER NOT NULL,file_inode INTEGER NOT NULL,state TEXT NOT NULL DEFAULT 'pending',media_id INTEGER,sha256 TEXT)");
    expire();
}
void Uploads::expire() {
    for (auto& row:db_.query("SELECT * FROM uploads WHERE expires<? OR owner NOT IN (SELECT id FROM users)",{now()})) {
        try { discard_staged(row); }
        catch (const Error&) { log("upload_cleanup_skipped",{{"upload_id",row["id"]}},"error"); }
        db_.query("DELETE FROM uploads WHERE id=?",Json::array({row["id"]}));
    }
}
void Uploads::discard_staged(const Json& row) {
    if (row["state"]!="pending") return;
    FilePtr dir;
    try { dir=directory(row["root"].get<std::string>()); }
    catch (const Error& error) { if (error.status==404) return; throw; }
    auto identity=dir->stat();
    if (static_cast<long long>(identity.st_dev)!=row["device"] || static_cast<long long>(identity.st_ino)!=row["inode"]) throw Error(409,"upload directory changed; cleanup refused");
    auto name=".resume-"+row["id"].get<std::string>();
    struct stat stat{};
    if (::fstatat(dir->fd,name.c_str(),&stat,AT_SYMLINK_NOFOLLOW)!=0) {
        if (errno==ENOENT) return;
        throw Error(507,"cannot inspect staging file");
    }
    if (!S_ISREG(stat.st_mode) || stat.st_nlink!=1 || stat.st_dev!=identity.st_dev || static_cast<long long>(stat.st_ino)!=row["file_inode"]) throw Error(409,"staging file changed; cleanup refused");
    if (::unlinkat(dir->fd,name.c_str(),0)!=0 || ::fsync(dir->fd)!=0) throw Error(507,"cannot discard staging file");
}
Json Uploads::owned(const httplib::Request& request,bool mutation) {
    auto who=auth_.require(request,true,mutation);
    auto rows=db_.query("SELECT * FROM uploads WHERE id=? AND owner=?",{request.matches[1].str(),who.id});
    if (rows.empty()) throw Error(404,"upload not found for this account");
    if (rows[0]["expires"].get<long long>()<=now()) throw Error(410,"upload expired; start again");
    return rows[0];
}
FilePtr Uploads::folder(const Json& row) {
    if (media_.root()!=row["root"].get<std::string>()) throw Error(409,"shared root changed; discard upload and start again");
    auto dir=directory(row["root"].get<std::string>()); auto stat=dir->stat();
    if (static_cast<long long>(stat.st_dev)!=row["device"] || static_cast<long long>(stat.st_ino)!=row["inode"]) throw Error(409,"upload directory changed");
    return dir;
}
FilePtr Uploads::staged(const Json& row,const FilePtr& dir) {
    int fd=::openat(dir->fd,(".resume-"+row["id"].get<std::string>()).c_str(),O_RDWR|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK);
    if (fd<0) throw Error(409,"staging file missing; discard upload");
    auto file=std::make_shared<File>(fd); auto stat=file->stat();
    if (!S_ISREG(stat.st_mode) || stat.st_nlink!=1 || static_cast<long long>(stat.st_ino)!=row["file_inode"]) throw Error(409,"staging file changed");
    if (stat.st_size<row["offset"].get<long long>()) throw Error(409,"acknowledged bytes missing; discard upload");
    // Recover bytes written before a crash but never acknowledged in SQLite.
    if (stat.st_size>row["offset"].get<long long>() && ::ftruncate(fd,row["offset"].get<long long>())!=0) throw Error(507,"cannot recover staged upload");
    return file;
}
Json Uploads::status(const Json& row) {
    return {{"id",row["id"]},{"filename",row["name"]},{"size",row["size"]},{"offset",row["offset"]},{"fingerprint",row["fingerprint"]},{"chunk_size",chunk_size},{"expires",row["expires"]},{"state",row["state"]},{"media_id",row["media_id"]},{"sha256",row["sha256"]}};
}
void Uploads::preflight(const httplib::Request& request) {
    auth_.require(request,true,true);
    if (request.has_header("Transfer-Encoding")) throw Error(411,"chunk requires Content-Length");
    if (request.get_header_value("Content-Type")!="application/octet-stream" || request.has_header("Content-Encoding")) throw Error(415,"uncompressed binary chunk required");
    auto length=number_header(request,"Content-Length"); (void)number_header(request,"Upload-Offset");
    if (!length || length>chunk_size) throw Error(413,"chunk must contain 1..1048576 bytes");
}
void Uploads::routes(httplib::Server& server) {
    server.Post("/api/uploads",[this](const httplib::Request& request,httplib::Response& response) {
        auto who=auth_.require(request,true,true);
        if (request.body.size()>300*1024) throw Error(413,"upload manifest too large");
        auto input=body(request,300*1024);
        if (input.size()!=3 || !input.contains("filename") || !input.contains("size") || !input.contains("hashes") || !input["size"].is_number_integer() || !input["hashes"].is_array()) throw Error(400,"filename, integer size and SHA256 chunk hashes required");
        auto name=input["filename"].get<std::string>(); upload_name(name);
        auto length=input["size"].get<long long>();
        if (length<1 || static_cast<unsigned long long>(length)>config_.upload_limit) throw Error(413,"invalid upload size");
        auto count=(static_cast<size_t>(length)+chunk_size-1)/chunk_size;
        if (input["hashes"].size()!=count) throw Error(400,"manifest chunk count mismatch");
        std::string joined; for (auto& hash:input["hashes"]) { if (!hexhash(hash)) throw Error(400,"invalid SHA256 chunk hash"); joined+=hash.get<std::string>(); }
        std::lock_guard lock(mutex_); std::lock_guard operation(media_.operations_); expire();
        Transaction tx(db_); who=auth_.require(request,true,true);
        if (db_.query("SELECT count(*) AS n FROM uploads WHERE state='pending'")[0]["n"].get<int>()>=20 || db_.query("SELECT count(*) AS n FROM uploads WHERE owner=? AND state='pending'",{who.id})[0]["n"].get<int>()>=5) throw Error(429,"too many pending uploads; discard unused sessions");
        auto shared=media_.root(); auto dir=directory(shared);
        struct stat target{};
        if (::fstatat(dir->fd,name.c_str(),&target,AT_SYMLINK_NOFOLLOW)==0) throw Error(409,"upload destination exists or cannot be published");
        std::array<unsigned char,16> random{}; randombytes_buf(random.data(),random.size()); std::array<char,33> hex{}; sodium_bin2hex(hex.data(),hex.size(),random.data(),random.size()); std::string id=hex.data();
        auto temporary=".resume-"+id;
        auto fd=::openat(dir->fd,temporary.c_str(),O_CREAT|O_EXCL|O_RDWR|O_NOFOLLOW|O_CLOEXEC,0600);
        if (fd<0) throw Error(507,"cannot create staging file");
        auto file=std::make_shared<File>(fd); auto stat=dir->stat();
        try {
            if (::fsync(fd)!=0 || ::fsync(dir->fd)!=0) throw Error(507,"cannot sync upload creation");
            db_.query("INSERT INTO uploads(id,owner,root,name,size,hashes,fingerprint,expires,device,inode,file_inode) VALUES(?,?,?,?,?,?,?,?,?,?,?)",{id,who.id,shared,name,length,input["hashes"].dump(),sha(joined),now()+86400,static_cast<long long>(stat.st_dev),static_cast<long long>(stat.st_ino),static_cast<long long>(file->stat().st_ino)});
            tx.commit();
        } catch (...) { ::unlinkat(dir->fd,temporary.c_str(),0); throw; }
        reply(response,status(db_.query("SELECT * FROM uploads WHERE id=?",{id})[0]),201);
    });
    const std::string pattern="/api/uploads/([0-9a-f]{32})";
    server.Get(pattern,[this](const httplib::Request& request,httplib::Response& response) {
        std::lock_guard lock(mutex_); auto row=owned(request,false);
        if (row["state"]=="pending") { auto dir=folder(row); (void)staged(row,dir); }
        reply(response,status(row));
    });
    server.Put(pattern+"/chunk",[this](const httplib::Request& request,httplib::Response& response) {
        preflight(request);
        std::lock_guard lock(mutex_); std::lock_guard operation(media_.operations_);
        auto row=owned(request,true); if (row["state"]!="pending") throw Error(409,"upload already completed");
        auto offset=number_header(request,"Upload-Offset"),length=request.body.size(),total=row["size"].get<size_t>();
        if (offset%chunk_size || offset>=total || length!=std::min(chunk_size,total-offset)) throw Error(400,"chunk boundary or length mismatch");
        auto hashes=Json::parse(row["hashes"].get<std::string>());
        if (sha(request.body)!=hashes[offset/chunk_size].get<std::string>()) throw Error(422,"chunk SHA256 mismatch; no progress acknowledged");
        auto dir=folder(row); auto file=staged(row,dir); auto confirmed=row["offset"].get<size_t>();
        if (offset<confirmed) { reply(response,status(row)); return; } // A lost response may be retried safely.
        if (offset!=confirmed) throw Error(409,"offset mismatch; query server confirmed offset");
        size_t done=0;
        while (done<length) {
            auto n=::pwrite(file->fd,request.body.data()+done,length-done,static_cast<off_t>(offset+done));
            if (n<0 && errno==EINTR) continue;
            if (n<=0) throw Error(507,"chunk write failed; query confirmed offset");
            done+=static_cast<size_t>(n);
        }
        if (::fsync(file->fd)!=0) throw Error(507,"chunk sync failed");
        Transaction tx(db_); (void)owned(request,true);
        db_.query("UPDATE uploads SET offset=? WHERE id=?",{offset+length,row["id"].get<std::string>()}); tx.commit();
        row["offset"]=offset+length; reply(response,status(row));
    });
    server.Post(pattern+"/complete",[this](const httplib::Request& request,httplib::Response& response) {
        std::lock_guard lock(mutex_); std::lock_guard operation(media_.operations_); auto row=owned(request,true);
        if (row["state"]=="completed") { reply(response,{{"media",{{"id",row["media_id"]},{"name",row["name"]},{"size",row["size"]}}},{"sha256",row["sha256"]}},201); return; }
        if (row["offset"]!=row["size"]) throw Error(409,"upload has missing chunks");
        auto dir=folder(row); auto temporary=".resume-"+row["id"].get<std::string>(); auto name=row["name"].get<std::string>();
        FilePtr file; bool recovered=false;
        try { file=staged(row,dir); } catch (const Error&) {
            // Recovery for a crash between atomic rename and SQLite commit.
            file=regular_file(dir,name); if (static_cast<long long>(file->stat().st_ino)!=row["file_inode"]) throw Error(409,"publication cannot be recovered"); recovered=true;
        }
        auto hashes=Json::parse(row["hashes"].get<std::string>());
        for (size_t offset=0;offset<row["size"].get<size_t>();offset+=chunk_size) {
            std::string chunk(std::min(chunk_size,row["size"].get<size_t>()-offset),'\0');
            auto n=::pread(file->fd,chunk.data(),chunk.size(),static_cast<off_t>(offset));
            if (n!=static_cast<ssize_t>(chunk.size()) || sha(chunk)!=hashes[offset/chunk_size].get<std::string>()) throw Error(422,"staged data checksum failed; discard upload");
        }
        if (!container_signature(file,format_of(name))) throw Error(415,"file does not match a supported video container");
        auto probe=probe_video(file,config_.ffprobe); if (probe.state=="invalid") throw Error(415,"FFprobe found no readable video stream");
        auto checksum=full_hash(file,row["size"].get<size_t>());
        Transaction tx(db_); auto who=auth_.require(request,true,true); (void)folder(row);
        if (!recovered && ::syscall(SYS_renameat2,dir->fd,temporary.c_str(),dir->fd,name.c_str(),RENAME_NOREPLACE)!=0) throw Error(errno==EEXIST?409:507,"upload destination exists or cannot be published");
        try {
            db_.query("DELETE FROM media WHERE root=? AND relative=?",{row["root"].get<std::string>(),name});
            auto item=media_.register_file(row["root"].get<std::string>(),name,file,probe);
            db_.query("UPDATE uploads SET state='completed',media_id=?,sha256=? WHERE id=?",{item["id"].get<long long>(),checksum,row["id"].get<std::string>()});
            if (::fsync(dir->fd)!=0) throw Error(507,"cannot sync upload publication");
            media_.finish(tx,"media_uploaded_resumable",who.id,{{"media_id",item["id"]},{"bytes",row["size"]}});
            reply(response,{{"media",{{"id",item["id"]},{"name",name},{"size",row["size"]}}},{"sha256",checksum}},201);
        } catch (...) { if (!recovered) ::renameat(dir->fd,name.c_str(),dir->fd,temporary.c_str()); throw; }
    });
    server.Delete(pattern,[this](const httplib::Request& request,httplib::Response& response) {
        std::lock_guard lock(mutex_); auto row=owned(request,true);
        discard_staged(row);
        db_.query("DELETE FROM uploads WHERE id=?",Json::array({row["id"]})); reply(response,{{"discarded",true}});
    });
}
}
