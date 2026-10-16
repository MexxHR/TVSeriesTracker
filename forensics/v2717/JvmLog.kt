package android.util
/** JVM-only Android logging type boundary. */
object Log { @JvmStatic fun d(tag: String, message: String): Int = 0 }
