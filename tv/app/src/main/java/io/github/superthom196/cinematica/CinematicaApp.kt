package io.github.superthom196.cinematica

import android.app.Application
import coil3.ImageLoader
import coil3.SingletonImageLoader
import coil3.bitmapFactoryMaxParallelism
import coil3.disk.DiskCache
import coil3.disk.directory
import coil3.memory.MemoryCache
import coil3.network.okhttp.OkHttpNetworkFetcherFactory
import coil3.request.crossfade
import okhttp3.OkHttpClient
import java.util.concurrent.TimeUnit

class CinematicaApp : Application(), SingletonImageLoader.Factory {
    override fun newImageLoader(context: coil3.PlatformContext): ImageLoader {
        val http = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .build()
        return ImageLoader.Builder(context)
            .components {
                add(OkHttpNetworkFetcherFactory(callFactory = { http }))
            }
            // Two cores, so four decoder threads only fight the main thread during a scroll.
            .bitmapFactoryMaxParallelism(2)
            // Grid tiles snap in over a flat placeholder rather than fading in.
            .crossfade(false)
            // A guard on a 192 MB heap, hardware bitmaps mostly live outside it.
            .memoryCache { MemoryCache.Builder().maxSizePercent(context, 0.20).build() }
            .diskCache {
                DiskCache.Builder()
                    .directory(cacheDir.resolve("image_cache"))
                    .maxSizeBytes(64L * 1024 * 1024)
                    .build()
            }
            .build()
    }
}
