// Optional fast parser for large original MSRC CSVs. No SSD simulation.
#define NOMINMAX
#include <windows.h>
#include <bcrypt.h>
#include <algorithm>
#include <array>
#include <charconv>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

using U = unsigned long long;
U number(std::string_view text) {
    U value = 0;
    auto result = std::from_chars(text.data(), text.data()+text.size(), value);
    if (result.ec != std::errc() || result.ptr != text.data()+text.size()) throw std::runtime_error("invalid integer");
    return value;
}
std::string mask_hex(const std::vector<unsigned char>& mask) {
    const char* digits = "0123456789abcdef";
    std::string result;
    for (size_t nibble = (mask.size()+3)/4; nibble-- > 0;) {
        unsigned value = 0;
        for (size_t bit=0; bit<4; ++bit) if (nibble*4+bit < mask.size() && mask[nibble*4+bit]) value |= 1U<<bit;
        if (value || !result.empty()) result += digits[value];
    }
    return "0x" + (result.empty() ? "0" : result);
}
int wmain(int argc, wchar_t** argv) {
    try {
        if (argc != 5 && argc != 6) throw std::runtime_error("usage: scan file capacity_sectors zone_sectors nvme [allow_folding]");
        U capacity = std::stoull(argv[2]), zone = std::stoull(argv[3]);
        bool nvme = std::wstring(argv[4]) == L"1";
        bool allow_folding = argc == 5 || std::wstring(argv[5]) == L"1";
        if (!capacity || !zone || capacity%zone) throw std::runtime_error("expected full stripe-sized user zones");
        std::ifstream source(std::filesystem::path(argv[1]), std::ios::binary);
        if (!source) throw std::runtime_error("cannot open source");
        BCRYPT_ALG_HANDLE algorithm = nullptr;
        BCRYPT_HASH_HANDLE hash = nullptr;
        if (BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA256_ALGORITHM,nullptr,0)<0) throw std::runtime_error("SHA256 provider failed");
        DWORD object_size=0, received=0;
        BCryptGetProperty(algorithm,BCRYPT_OBJECT_LENGTH,reinterpret_cast<PUCHAR>(&object_size),sizeof(object_size),&received,0);
        std::vector<unsigned char> hash_object(object_size);
        if (BCryptCreateHash(algorithm,&hash,hash_object.data(),object_size,nullptr,0,0)<0) throw std::runtime_error("SHA256 init failed");
        std::vector<unsigned char> all(capacity/zone), writes(capacity/zone), unfolded(capacity/zone);
        std::map<std::string,U> stats={{"requests",0},{"reads",0},{"writes",0},{"read_bytes",0},{"write_bytes",0},
            {"runtime_remapped_requests",0},{"crosses_capacity",0},{"oversized_requests",0},{"out_of_order_timestamps",0},
            {"out_of_range_requests",0},{"zone_state_updates",0},{"zone_skip_requests",0},{"zone_new_requests",0}};
        std::set<std::pair<std::string,std::string>> devices;
        U previous=0, max_end=0, row=0;
        auto mark = [&](std::vector<unsigned char>& mask,U first,U last) {
            U added=0;
            if (first==last && mask[first]) return added;
            for (U id=first;id<=last;++id) if (!mask[id]) { mask[id]=1; ++added; }
            return added;
        };
        auto& requests=stats["requests"];
        auto& read_count=stats["reads"];
        auto& write_count=stats["writes"];
        auto& read_bytes=stats["read_bytes"];
        auto& write_bytes=stats["write_bytes"];
        auto& time_errors=stats["out_of_order_timestamps"];
        auto& updates=stats["zone_state_updates"];
        auto& skipped=stats["zone_skip_requests"];
        auto& new_requests=stats["zone_new_requests"];
        std::string previous_host, previous_disk;
        auto line = [&](std::string_view text) {
            ++row;
            if (!text.empty() && text.back()=='\r') text.remove_suffix(1);
            std::array<std::string_view,7> fields;
            size_t start=0;
            for (size_t i=0;i<7;++i) {
                size_t comma=text.find(',',start);
                if ((i<6 && comma==std::string_view::npos)||(i==6 && comma!=std::string_view::npos)) throw std::runtime_error("expected 7 CSV fields");
                fields[i]=text.substr(start,comma==std::string_view::npos?comma:comma-start);
                start=comma+1;
            }
            U stamp=number(fields[0]), offset=number(fields[4]), size=number(fields[5]);
            bool write=fields[3]=="Write";
            if ((!write && fields[3]!="Read") || offset%512 || !size || size%512) throw std::runtime_error("invalid original request");
            if (fields[1]!=previous_host || fields[2]!=previous_disk) {
                previous_host=fields[1]; previous_disk=fields[2];
                devices.emplace(previous_host,previous_disk);
            }
            time_errors += stamp<previous;
            previous=stamp;
            ++requests;
            ++(write?write_count:read_count);
            (write?write_bytes:read_bytes) += size;
            max_end=std::max(max_end,offset+size);
            U lba=offset/512,sectors=size/512;
            if (sectors>65535) { ++stats["oversized_requests"]; return; }
            if (lba+sectors>capacity) {
                ++stats["out_of_range_requests"];
                if (!allow_folding) return;
            }
            if (lba+sectors<=capacity) mark(unfolded,lba/zone,(lba+sectors-1)/zone);
            if (lba>=capacity) { ++stats["runtime_remapped_requests"]; lba %= capacity-1; }
            if (nvme) lba=((lba>>32)<<31)|(lba&0xffffffffULL);
            if (lba+sectors>capacity) { ++stats["crosses_capacity"]; return; }
            U first=lba/zone, last=(lba+sectors-1)/zone;
            U added=mark(all,first,last);
            updates+=added;
            if (added) ++new_requests; else ++skipped;
            if (write) mark(writes,first,last);
        };
        std::vector<char> buffer(4*1024*1024);
        std::string pending;
        while (source) {
            source.read(buffer.data(),buffer.size());
            size_t length=static_cast<size_t>(source.gcount());
            if (BCryptHashData(hash,reinterpret_cast<PUCHAR>(buffer.data()),static_cast<ULONG>(length),0)<0) throw std::runtime_error("SHA256 update failed");
            size_t start=0;
            for (size_t end=0;end<length;++end) if (buffer[end]=='\n') {
                if (pending.empty()) line(std::string_view(buffer.data()+start,end-start));
                else { pending.append(buffer.data()+start,end-start); line(pending); pending.clear(); }
                start=end+1;
            }
            pending.append(buffer.data()+start,length-start);
        }
        if (!source.eof()) throw std::runtime_error("source read failed");
        if (!pending.empty()) line(pending);
        std::array<unsigned char,32> digest;
        if (BCryptFinishHash(hash,digest.data(),digest.size(),0)<0) throw std::runtime_error("SHA256 finish failed");
        BCryptDestroyHash(hash);
        BCryptCloseAlgorithmProvider(algorithm,0);
        std::cout << "{";
        for (const auto& value:stats) std::cout << '"' << value.first << "\":" << value.second << ',';
        std::cout << "\"sha256\":\"";
        for (auto byte:digest) std::cout << "0123456789abcdef"[byte>>4] << "0123456789abcdef"[byte&15];
        std::cout << "\",\"max_original_end_bytes\":" << max_end << ",\"devices\":[";
        bool first=true;
        for (const auto& d:devices) {
            // Original MSRC host/disk names are ASCII identifiers.
            for (char c:d.first+d.second) if (c=='"'||c=='\\'||c<32) throw std::runtime_error("invalid host/disk identifier");
            if (!first) std::cout << ',';
            first=false;
            std::cout << "[\"" << d.first << "\",\"" << d.second << "\"]";
        }
        std::cout << "],\"valid_for_native_replay\":" << (stats["crosses_capacity"]||stats["oversized_requests"]||stats["out_of_order_timestamps"]?"false":"true") << ",\"coverage\":{";
        first=true;
        const std::array<std::pair<const char*,std::vector<unsigned char>*>,3> masks = {{{"read_write",&all},{"writes",&writes},{"unfolded",&unfolded}}};
        for (const auto& pair: masks) {
            if (!first) std::cout << ',';
            first=false;
            std::cout << '"' << pair.first << "\":{\"used_zones\":" << std::count(pair.second->begin(),pair.second->end(),1)
                      << ",\"mask_hex\":\"" << mask_hex(*pair.second) << "\"}";
        }
        std::cout << "}}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
