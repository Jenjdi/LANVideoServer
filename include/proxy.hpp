#pragma once
#include "core.hpp"
namespace lv {
Json validate_proxy(const Json& input);
Json proxy_summary(const Json& input);
Json test_proxy(const Json& proxy,const Json& target);
}
