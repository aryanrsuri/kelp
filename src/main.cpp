#include <iostream>
#include <string>
#include <openssl/evp.h>
using namespace std;

int main(void)
{
        string str = "Meet me";
        unsigned char hash[EVP_MAX_MD_SIZE];
        size_t hash_len = 0;

        auto r_digest = EVP_Q_digest(NULL, "SHA256", NULL, str.c_str(), str.size(), hash, &hash_len);
        if (r_digest != 1) {
                cerr << "Digest failed\n";
                return 1;
        }
        for (size_t i =0; i < hash_len; ++i)
                cout << hex << (int)hash[i];
        cout << '\n';

        // For simple hash compare and keys...
        // size_t str_hash = hash<string>{}(str);
        // cout << str_hash << '\n';
}
