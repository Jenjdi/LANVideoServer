#pragma once
#include <httplib.h>

namespace lv {
// Adds request deadlines to httplib's per-read inactivity timeout.
class HttpServer final : public httplib::Server {
    bool process_and_close_socket(socket_t socket) override;
public:
    static void allow_upload_body();
};
}
