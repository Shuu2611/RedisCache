#!/usr/bin/env python3
import json

try:
    import lz4.frame
    HAS_LZ4 = True
except ImportError:
    HAS_LZ4 = False

import zlib


class CompressionEngine:
    
    def __init__(self, algorithm='lz4'):
        self.algorithm = algorithm
        
        if algorithm == 'lz4' and not HAS_LZ4:
            print("LZ4 not available, falling back to zlib")
            self.algorithm = 'zlib'
    
    def compress(self, hash_data):
        clean_data = {}
        for k, v in hash_data.items():
            key = k.decode('utf-8') if isinstance(k, bytes) else str(k)
            if isinstance(v, bytes):
                clean_data[key] = v.decode('utf-8')
            else:
                clean_data[key] = str(v)
        
        json_str = json.dumps(clean_data, ensure_ascii=False)
        json_bytes = json_str.encode('utf-8')
        
        if self.algorithm == 'lz4':
            compressed = lz4.frame.compress(json_bytes)
        else:
            compressed = zlib.compress(json_bytes, level=6)
        return compressed
    
    def decompress(self, compressed_data):
        if self.algorithm == 'lz4':
            json_bytes = lz4.frame.decompress(compressed_data)
        else:
            json_bytes = zlib.decompress(compressed_data)
        
        json_str = json_bytes.decode('utf-8')
        hash_data = json.loads(json_str)
        return hash_data
    
    def get_compression_stats(self, original_size, compressed_size):
        ratio = (1 - compressed_size / original_size) * 100
        return {
            'original': original_size,
            'compressed': compressed_size,
            'ratio': f"{ratio:.1f}%",
            'savings': original_size - compressed_size
        }


def estimate_size(hash_data):
    json_str = json.dumps(hash_data, ensure_ascii=False)
    return len(json_str.encode('utf-8'))


def create_compressor(algorithm='lz4'):
    return CompressionEngine(algorithm)
