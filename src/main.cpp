#include "core.hpp"
#include "auth.hpp"
#include "media.hpp"
#include "uploads.hpp"
#include "bt.hpp"
#include "remote.hpp"
#include "shares.hpp"
#include "monitor.hpp"
#include "http_server.hpp"
#include <sodium.h>
#include <atomic>
#include <csignal>
#include <thread>
#include <sys/stat.h>
#include <sys/socket.h>
#include <charconv>
static volatile std::sig_atomic_t shutting_down = 0;
static void signal_stop(int) { shutting_down = 1; }
int main(int argc, char** argv) {
    using namespace lv;
    ::umask(0077);
    try {
        bool init_stdin=argc==2 && std::string(argv[1])=="--init-admin-stdin";
        bool init_interactive=argc==2 && std::string(argv[1])=="--init-admin";
        if (argc>1 && !init_stdin && !init_interactive) throw Error(400,"supported options: --init-admin or --init-admin-stdin");
        if (sodium_init() < 0) throw Error(500,"crypto initialization failed");
        auto config = Config::load();
        Database database(config.data / "lanvideo.db");
        Auth auth(database,config);
        if (init_stdin || init_interactive) {
            auth.initialize(read_admin_input(init_stdin));
            return 0;
        }
        Media media(database,auth,config);
        Uploads uploads(database,auth,media,config);
        Remote remote(auth,config);
        Shares shares(database,auth,media,config);
        HttpServer server;
        server.set_socket_options([](int socket) {
            int enabled = 1, disabled = 0;
            setsockopt(socket, SOL_SOCKET, SO_REUSEADDR, &enabled, sizeof(enabled));
            setsockopt(socket, SOL_SOCKET, SO_REUSEPORT, &disabled, sizeof(disabled));
        });
        // Reserve the HTTP port before libtorrent asks the kernel for an ephemeral port.
        if (!server.bind_to_port(config.bind,config.port)) throw Error(500,"cannot bind server");
        Bt bt(database,auth,config);
        Monitor monitor(database,auth,config);
        server.new_task_queue = [] { return new httplib::ThreadPool(8, 64); };
        server.set_payload_max_length(std::max(config.upload_limit,size_t(8*1024*1024)));
        server.set_read_timeout(10,0);
        server.set_write_timeout(10,0);
        server.set_keep_alive_max_count(50);
        server.set_exception_handler([](const auto&, auto& response, std::exception_ptr ep) {
            try { std::rethrow_exception(ep); }
            catch (const Error& e) { reply(response, {{"error",e.what()}},e.status); }
            catch (const Json::exception& error) {
                log("json_type_error",{{"exception_id",error.id}},"error");
                reply(response, {{"error","invalid input type"}},400);
            }
            catch (const std::exception&) { reply(response, {{"error","internal error"}},500); log("internal_error", Json::object(), "error"); }
        });
        server.set_error_handler([&](const auto& request, auto& response) {
            if (response.status==416 && response.body.empty()) {
                try { remote.guard(request); reject_duplicate_query(request); }
                catch (const Error& error) { const_cast<httplib::Request&>(request).ranges.clear(); reply(response,{{"error",error.what()}},error.status); }
            }
            if (response.status==416 && response.body.empty()) media.range_failure(request,response);
            if (response.status==416 && response.body.empty()) bt.range_failure(request,response);
            if (response.status==416 && response.body.empty()) shares.range_failure(request,response);
            if (response.body.empty()) reply(response, {{"error", "HTTP request rejected"}}, response.status);
        });
        server.set_post_routing_handler([&](const auto&,auto& response) {
            if (config.remote_mode=="reverse_proxy") response.set_header("Strict-Transport-Security","max-age=31536000");
            response.set_header("X-Content-Type-Options","nosniff");
            response.set_header("Cache-Control","no-store");
            response.set_header("Referrer-Policy","no-referrer");
            response.set_header("X-Frame-Options","DENY");
            response.set_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; object-src 'none'");
        });
        auto preflight=[&](const httplib::Request& request) {
            remote.guard(request);
            reject_duplicate_query(request);
            if (request.method=="POST" && request.path=="/api/torrents") { bt.preflight(request); HttpServer::allow_upload_body(); return; }
            if (request.method=="PUT" && request.path.starts_with("/api/uploads/")) { uploads.preflight(request); HttpServer::allow_upload_body(); return; }
            if (request.path=="/api/media/upload" && request.method=="POST") { media.upload_preflight(request); HttpServer::allow_upload_body(); return; }
            if (request.has_header("Content-Encoding") && request.get_header_value("Content-Encoding")!="identity") throw Error(415,"compressed requests unsupported");
            if (request.has_header("Transfer-Encoding")) throw Error(411,"non-upload requests require Content-Length instead of chunked encoding");
            if (request.has_header("Content-Length")) {
                if (request.get_header_value_count("Content-Length")!=1) throw Error(400,"ambiguous Content-Length");
                auto text=request.get_header_value("Content-Length"); size_t length=0;
                auto parsed=std::from_chars(text.data(),text.data()+text.size(),length);
                if (parsed.ec!=std::errc{} || parsed.ptr!=text.data()+text.size()) throw Error(400,"invalid Content-Length");
                if (length>8*1024*1024) throw Error(413,"non-upload request exceeds 8 MiB");
            }
        };
        server.set_pre_routing_handler([&](const auto& request,auto& response) {
            try { preflight(request); return httplib::Server::HandlerResponse::Unhandled; }
            catch (const Error& error) { response.set_header("Connection","close"); reply(response,{{"error",error.what()}},error.status); return httplib::Server::HandlerResponse::Handled; }
        });
        server.set_expect_100_continue_handler([&](const auto& request,auto& response) {
            try { preflight(request); return 100; }
            catch (const Error& error) { reply(response,{{"error",error.what()}},error.status); return error.status; }
        });
        server.Get("/api/health",[&](const auto&, auto& response) {
            database.query("SELECT 1"); reply(response,{{"status","ok"},{"version",LANVIDEO_VERSION}});
        });
        auth.routes(server);
        media.routes(server);
        uploads.routes(server);
        bt.routes(server);
        remote.routes(server);
        shares.routes(server);
        monitor.routes(server);
        std::signal(SIGINT, signal_stop); std::signal(SIGTERM, signal_stop);
        std::jthread stopper([&](std::stop_token token) {
            while (!token.stop_requested() && !shutting_down) std::this_thread::sleep_for(std::chrono::milliseconds(50));
            if (shutting_down) { bt.stop_streams(); server.stop(); }
        });
        log("server_start",{{"bind",config.bind},{"port",config.port},{"admin_initialized",auth.initialized()},{"transport","HTTP; local use only unless TLS reverse proxy"}});
        bool ok = server.listen_after_bind();
        stopper.request_stop();
        log("server_stop");
        return ok ? 0 : 1;
    } catch (const std::exception& e) { log("startup_failure",{{"reason",e.what()}},"error"); return 1; }
}
