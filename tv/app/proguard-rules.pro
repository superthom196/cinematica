# kotlinx.serialization: keep generated serializers for our models.
-keepclassmembers class io.github.superthom196.cinematica.api.** { *** Companion; }
-keepclasseswithmembers class io.github.superthom196.cinematica.api.** { kotlinx.serialization.KSerializer serializer(...); }
-keep,includedescriptorclasses class io.github.superthom196.cinematica.api.**$$serializer { *; }
-dontwarn org.slf4j.**
-dontwarn javax.annotation.**

# libVLC: reflection-heavy JNI bridge, must survive shrinking/obfuscation untouched.
-keep class org.videolan.libvlc.** { *; }
-keep class org.videolan.libvlc.interfaces.** { *; }
