package com.joycon2studio

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import androidx.core.content.FileProvider
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONException
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/**
 * Finds a newer Android build on GitHub and installs it, as the Windows app does for its exe.
 *
 * Only this project's releases are used. A release that carries an Android APK also carries SHA256SUMS.txt. The
 * APK is checked against that file, and against its own package name and version, before the system installer
 * opens. Android installs an update only when the APK is signed with the same key as the installed app. The
 * parsing and the version and checksum rules are plain functions, so the unit tests cover them without a network.
 */
object AppUpdate {
    private const val RELEASES_URL =
        "https://api.github.com/repos/lexluther1307-sudo/Joy-con-2-Studio/releases?per_page=100"
    private const val DOWNLOAD_PREFIX = "https://github.com/lexluther1307-sudo/Joy-con-2-Studio/releases/download/"
    private const val CHECKSUMS_NAME = "SHA256SUMS.txt"
    private const val USER_AGENT = "joycon2studio-android"
    private val APK_NAME = Regex("""joycon2studio-android-(\d+\.\d+\.\d+)-debug\.apk""")

    /** The newest Android build that a release offers, with the two files needed to install it. */
    class Found(val version: String, val apkName: String, val apkUrl: String, val sumsUrl: String)

    sealed class Check {
        class Newer(val found: Found) : Check()
        object UpToDate : Check()
        object NoBuild : Check()
        object Failed : Check()
    }

    sealed class Download {
        class Ready(val file: File) : Download()
        object Mismatch : Download()
        object NoChecksum : Download()
        object Failed : Download()
    }

    /** Asks GitHub for the newest Android build and compares it with the installed version. */
    suspend fun check(installed: String): Check = withContext(Dispatchers.IO) {
        val json = fetch(RELEASES_URL, "application/vnd.github+json") ?: return@withContext Check.Failed
        val found = (try {
            newest(json)
        } catch (e: JSONException) {
            return@withContext Check.Failed
        }) ?: return@withContext Check.NoBuild
        if (isNewer(found.version, installed)) Check.Newer(found) else Check.UpToDate
    }

    /** The highest version among this project's releases that carry an Android APK and a checksum file, or null. */
    fun newest(releasesJson: String): Found? {
        val releases = JSONArray(releasesJson)
        var best: Found? = null
        for (i in 0 until releases.length()) {
            val release = releases.optJSONObject(i) ?: continue
            if (release.optBoolean("draft", false)) continue
            val assets = release.optJSONArray("assets") ?: continue
            var apkName = ""
            var apkUrl = ""
            var version = ""
            var sumsUrl = ""
            for (j in 0 until assets.length()) {
                val asset = assets.optJSONObject(j) ?: continue
                val name = asset.optString("name")
                val url = asset.optString("browser_download_url")
                val match = APK_NAME.matchEntire(name)
                // the highest APK in the release wins, whatever the order of the assets
                if (match != null && isProjectUrl(url) && (version.isEmpty() || isNewer(match.groupValues[1], version))) {
                    version = match.groupValues[1]
                    apkName = name
                    apkUrl = url
                }
                if (name == CHECKSUMS_NAME && isProjectUrl(url)) sumsUrl = url
            }
            if (version.isEmpty() || sumsUrl.isEmpty()) continue
            val current = best
            if (current == null || isNewer(version, current.version)) {
                best = Found(version, apkName, apkUrl, sumsUrl)
            }
        }
        return best
    }

    private fun isProjectUrl(url: String): Boolean = url.startsWith(DOWNLOAD_PREFIX)

    /** True when candidate is a higher version than current, comparing the dotted numbers in order. */
    fun isNewer(candidate: String, current: String): Boolean {
        val a = numbers(candidate)
        val b = numbers(current)
        for (i in 0 until maxOf(a.size, b.size)) {
            val x = a.getOrElse(i) { 0 }
            val y = b.getOrElse(i) { 0 }
            if (x != y) return x > y
        }
        return false
    }

    /** The dotted numbers of a version. Anything after a dash or a plus sign (a pre-release tag) is left out. */
    private fun numbers(version: String): List<Int> =
        version.substringBefore('-').substringBefore('+').split('.').map { it.toIntOrNull() ?: 0 }

    /** The SHA-256 that a checksum file gives for one file name, from sha256sum style lines, or null. */
    fun sha256For(sums: String, name: String): String? {
        for (line in sums.lineSequence()) {
            val text = line.trim().removePrefix("﻿")
            if (text.length <= 64) continue
            val hash = text.substring(0, 64)
            val file = text.substring(64).trimStart(' ', '*')
            val isHex = hash.all { it in '0'..'9' || it.lowercaseChar() in 'a'..'f' }
            if (file == name && isHex) return hash.lowercase()
        }
        return null
    }

    /**
     * Downloads the APK into the app's cache and checks it before anything is installed: its SHA-256 must match the
     * release's checksum file, and it must name this app and the version it was listed as.
     */
    suspend fun download(context: Context, found: Found, onProgress: (Int) -> Unit): Download =
        withContext(Dispatchers.IO) {
            val sums = fetch(found.sumsUrl, "text/plain") ?: return@withContext Download.Failed
            val expected = sha256For(sums, found.apkName) ?: return@withContext Download.NoChecksum
            val folder = File(context.cacheDir, "updates").apply { mkdirs() }
            folder.listFiles()?.forEach { it.delete() }
            val target = File(folder, found.apkName)
            val digest = MessageDigest.getInstance("SHA-256")
            var connection: HttpURLConnection? = null
            try {
                connection = URL(found.apkUrl).openConnection() as HttpURLConnection
                connection.connectTimeout = 15_000
                connection.readTimeout = 30_000
                connection.setRequestProperty("User-Agent", USER_AGENT)
                if (connection.responseCode != HttpURLConnection.HTTP_OK) {
                    target.delete()
                    return@withContext Download.Failed
                }
                val total = connection.contentLengthLong
                var reported = -1
                connection.inputStream.use { input ->
                    target.outputStream().use { output ->
                        val buffer = ByteArray(64 * 1024)
                        var done = 0L
                        while (true) {
                            ensureActive() // stops the download when the screen has gone
                            val count = input.read(buffer)
                            if (count < 0) break
                            output.write(buffer, 0, count)
                            digest.update(buffer, 0, count)
                            done += count
                            if (total > 0) {
                                val percent = (done * 100 / total).toInt()
                                if (percent != reported) {
                                    reported = percent
                                    onProgress(percent)
                                }
                            }
                        }
                    }
                }
            } catch (e: CancellationException) {
                target.delete()
                throw e
            } catch (e: Exception) {
                target.delete()
                return@withContext Download.Failed
            } finally {
                connection?.disconnect()
            }
            val actual = digest.digest().joinToString("") { (it.toInt() and 0xFF).toString(16).padStart(2, '0') }
            if (!actual.equals(expected, ignoreCase = true)) {
                target.delete()
                return@withContext Download.Mismatch
            }
            val info = context.packageManager.getPackageArchiveInfo(target.path, 0)
            if (info == null || info.packageName != context.packageName || info.versionName != found.version) {
                target.delete()
                return@withContext Download.Failed
            }
            Download.Ready(target)
        }

    /** True once the user has allowed this app to install other apps (Android 8.0 and newer). */
    fun canInstall(context: Context): Boolean = context.packageManager.canRequestPackageInstalls()

    /** Opens the screen where the user allows installs from this app. False when no such screen can open. */
    fun openUnknownSourcesSettings(context: Context): Boolean =
        start(context, Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${context.packageName}")))

    /** Opens the system installer for the downloaded APK, which asks the user to confirm. False when it cannot open. */
    fun install(context: Context, apk: File): Boolean {
        val uri = try {
            FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", apk)
        } catch (e: IllegalArgumentException) {
            return false
        }
        val intent = Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        return start(context, intent)
    }

    private fun start(context: Context, intent: Intent): Boolean {
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            context.startActivity(intent)
            true
        } catch (e: ActivityNotFoundException) {
            false
        }
    }

    /** The body of a GitHub URL as text, or null when it cannot be read. */
    private fun fetch(url: String, accept: String): String? {
        var connection: HttpURLConnection? = null
        return try {
            connection = URL(url).openConnection() as HttpURLConnection
            connection.connectTimeout = 10_000
            connection.readTimeout = 20_000
            connection.setRequestProperty("Accept", accept)
            connection.setRequestProperty("User-Agent", USER_AGENT)
            if (connection.responseCode != HttpURLConnection.HTTP_OK) {
                null
            } else {
                connection.inputStream.bufferedReader().use { it.readText() }
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            null
        } finally {
            connection?.disconnect()
        }
    }
}
