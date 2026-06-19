import asyncio
import os
import hashlib
import time

def generate_dummy_file(filename, size_mb):
    with open(filename, 'wb') as f:
        f.write(os.urandom(size_mb * 1024 * 1024))

async def bench_sync():
    def _hash_file(f_path: str):
        with open(f_path, "rb") as f:
            chunk = f.read(1024 * 1024)
            return hashlib.sha256(chunk).hexdigest()

    loop = asyncio.get_running_loop()
    import concurrent.futures

    start_time = time.time()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        tasks = [
            loop.run_in_executor(pool, _hash_file, f"dummy_{i}.bin")
            for i in range(5)
        ]
        await asyncio.gather(*tasks)
    return time.time() - start_time

async def bench_async():
    import aiofiles
    async def _hash_file(f_path: str):
        async with aiofiles.open(f_path, "rb") as f:
            chunk = await f.read(1024 * 1024)
            return hashlib.sha256(chunk).hexdigest()

    start_time = time.time()
    tasks = [
        _hash_file(f"dummy_{i}.bin")
        for i in range(5)
    ]
    await asyncio.gather(*tasks)
    return time.time() - start_time

async def main():
    for i in range(5):
        generate_dummy_file(f"dummy_{i}.bin", 50)

    # Warmup
    await bench_sync()
    await bench_async()

    sync_time = await bench_sync()
    async_time = await bench_async()

    print(f"Sync time (ThreadPoolExecutor): {sync_time:.4f}s")
    print(f"Async time (aiofiles): {async_time:.4f}s")

    for i in range(5):
        os.remove(f"dummy_{i}.bin")

if __name__ == "__main__":
    asyncio.run(main())
