package com.joycon2studio

import org.json.JSONException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The update rules: which release wins, what counts as newer, and which checksum belongs to the APK. */
class AppUpdateTest {
    private val base = "https://github.com/lexluther1307-sudo/Joy-con-2-Studio/releases/download"
    private val releases = """
        [
          {"tag_name": "v0.2.21", "draft": true, "assets": [
            {"name": "joycon2studio-android-0.1.5-debug.apk", "browser_download_url": "$base/v0.2.21/joycon2studio-android-0.1.5-debug.apk"},
            {"name": "SHA256SUMS.txt", "browser_download_url": "$base/v0.2.21/SHA256SUMS.txt"}]},
          {"tag_name": "v0.2.22", "draft": false, "assets": [
            {"name": "joycon2studio-android-0.1.9-debug.apk", "browser_download_url": "$base/v0.2.22/joycon2studio-android-0.1.9-debug.apk"}]},
          {"tag_name": "v0.9.0", "draft": false, "assets": [
            {"name": "joycon2studio-android-0.9.0-debug.apk", "browser_download_url": "https://example.test/0.9.0.apk"},
            {"name": "SHA256SUMS.txt", "browser_download_url": "https://example.test/sums.txt"}]},
          {"tag_name": "v0.2.20", "draft": false, "assets": [
            {"name": "JoyCon2Studio.exe", "browser_download_url": "$base/v0.2.20/JoyCon2Studio.exe"},
            {"name": "joycon2studio-android-0.1.1-debug.apk", "browser_download_url": "$base/v0.2.20/joycon2studio-android-0.1.1-debug.apk"},
            {"name": "joycon2studio-android-0.1.0-debug.apk", "browser_download_url": "$base/v0.2.20/joycon2studio-android-0.1.0-debug.apk"},
            {"name": "SHA256SUMS.txt", "browser_download_url": "$base/v0.2.20/SHA256SUMS.txt"}]},
          {"tag_name": "v0.2.19", "draft": false, "assets": [
            {"name": "joycon2studio-android-0.1.0-debug.apk", "browser_download_url": "$base/v0.2.19/joycon2studio-android-0.1.0-debug.apk"},
            {"name": "SHA256SUMS.txt", "browser_download_url": "$base/v0.2.19/SHA256SUMS.txt"}]},
          {"tag_name": "v0.2.18", "draft": false, "assets": [
            {"name": "JoyCon2Studio.exe", "browser_download_url": "$base/v0.2.18/JoyCon2Studio.exe"}]}
        ]
    """.trimIndent()

    private val fakeHash = "0123456789abcdef".repeat(4) // 64 characters

    @Test
    fun newestPicksTheHighestPublishedProjectBuildWithChecksums() {
        val found = AppUpdate.newest(releases)!!
        assertEquals("0.1.1", found.version)
        assertEquals("joycon2studio-android-0.1.1-debug.apk", found.apkName)
        assertEquals("$base/v0.2.20/joycon2studio-android-0.1.1-debug.apk", found.apkUrl)
        assertEquals("$base/v0.2.20/SHA256SUMS.txt", found.sumsUrl)
    }

    @Test
    fun noReleaseWithAnApkGivesNothing() {
        assertNull(AppUpdate.newest("""[{"tag_name": "v0.2.18", "draft": false, "assets": []}]"""))
        assertNull(AppUpdate.newest("[]"))
    }

    @Test(expected = JSONException::class)
    fun malformedJsonIsRefused() {
        AppUpdate.newest("not json")
    }

    @Test
    fun versionsCompareAsNumbers() {
        assertTrue(AppUpdate.isNewer("0.1.1", "0.1.0"))
        assertTrue(AppUpdate.isNewer("0.2.0", "0.1.9"))
        assertTrue(AppUpdate.isNewer("0.10.0", "0.9.0"))
        assertTrue(AppUpdate.isNewer("0.1.1-rc", "0.1.0"))
        assertFalse(AppUpdate.isNewer("0.1.0", "0.1.0"))
        assertFalse(AppUpdate.isNewer("0.1.0", "0.1.1"))
        assertFalse(AppUpdate.isNewer("0.1", "0.1.0"))
    }

    @Test
    fun checksumFilesGiveTheHashForTheRightName() {
        val sums = """
            65ff5f591be2306d16b0bc4a841e9fc30eb019ca94205e8e1c669e7b2f309b82  JoyCon2Studio.exe
            ${fakeHash.uppercase()} *joycon2studio-android-0.1.1-debug.apk
        """.trimIndent()
        assertEquals(
            "65ff5f591be2306d16b0bc4a841e9fc30eb019ca94205e8e1c669e7b2f309b82",
            AppUpdate.sha256For(sums, "JoyCon2Studio.exe"),
        )
        assertEquals(fakeHash, AppUpdate.sha256For(sums, "joycon2studio-android-0.1.1-debug.apk"))
        assertNull(AppUpdate.sha256For(sums, "missing.apk"))
    }

    @Test
    fun aByteOrderMarkDoesNotHideTheFirstLine() {
        val sums = "﻿$fakeHash  joycon2studio-android-0.1.1-debug.apk\n"
        assertEquals(fakeHash, AppUpdate.sha256For(sums, "joycon2studio-android-0.1.1-debug.apk"))
    }
}
