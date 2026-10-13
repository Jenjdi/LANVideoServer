// Test-only real libtorrent metadata producer and independently hashed TCP seeder.
#include <libtorrent/session.hpp>
#include <libtorrent/session_params.hpp>
#include <libtorrent/settings_pack.hpp>
#include <libtorrent/create_torrent.hpp>
#include <libtorrent/bencode.hpp>
#include <libtorrent/torrent_info.hpp>
#include <libtorrent/torrent_flags.hpp>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <thread>
#include <algorithm>
#include <csignal>
namespace lt=libtorrent;
static volatile std::sig_atomic_t stopped=0;
int main(int argc,char** argv) {
    try {
        if (argc<4) return 2;
        if (std::string(argv[1])=="make" && argc==5) {
            auto path=std::filesystem::path(argv[2]); lt::file_storage files;
            if (std::filesystem::is_directory(path)) {
                std::vector<std::filesystem::path> paths;
                for (auto& entry:std::filesystem::recursive_directory_iterator(path)) if (entry.is_regular_file()) paths.push_back(entry.path());
                std::sort(paths.begin(),paths.end());
                for (auto& file:paths) files.add_file((path.filename()/file.lexically_relative(path)).string(),static_cast<std::int64_t>(std::filesystem::file_size(file)));
            } else files.add_file(path.filename().string(),static_cast<std::int64_t>(std::filesystem::file_size(path)));
            lt::create_torrent torrent(files,65536,lt::create_torrent::v1_only);
            torrent.add_tracker(argv[4]); torrent.set_priv(true);
            lt::error_code error; lt::set_piece_hashes(torrent,path.parent_path().string(),error);
            if (error) return 3;
            auto entry=torrent.generate(); std::vector<char> bytes; lt::bencode(std::back_inserter(bytes),entry);
            std::ofstream out(argv[3],std::ios::binary); out.write(bytes.data(),static_cast<std::streamsize>(bytes.size())); return out?0:4;
        }
        lt::settings_pack config;
        config.set_str(lt::settings_pack::listen_interfaces,"127.0.0.1:0");
        config.set_bool(lt::settings_pack::enable_dht,false); config.set_bool(lt::settings_pack::enable_lsd,false);
        config.set_bool(lt::settings_pack::enable_upnp,false); config.set_bool(lt::settings_pack::enable_natpmp,false);
        config.set_bool(lt::settings_pack::enable_outgoing_utp,false); config.set_bool(lt::settings_pack::enable_incoming_utp,false);
        lt::session session{lt::session_params(config)};
        lt::add_torrent_params params; params.ti=std::make_shared<lt::torrent_info>(argv[2]); params.save_path=argv[3];
        params.flags&=~(lt::torrent_flags::auto_managed|lt::torrent_flags::paused|lt::torrent_flags::seed_mode);
        auto handle=session.add_torrent(params);
        handle.set_upload_limit(argc>=5?std::stoi(argv[4]):512*1024);
        std::signal(SIGTERM,[](int){stopped=1;});
        std::signal(SIGINT,[](int){stopped=1;});
        for (int i=0;i<200;++i) {
            if (handle.status().is_seeding && session.listen_port()) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
        if (!handle.status().is_seeding || !session.listen_port()) return 5;
        std::cout<<"{\"ready\":true,\"port\":"<<session.listen_port()<<"}"<<std::endl;
        while (!stopped) std::this_thread::sleep_for(std::chrono::milliseconds(50));
        handle.pause(); return 0;
    } catch (...) { std::cerr<<"BT fixture failed\n"; return 1; }
}
