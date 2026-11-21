package com.joycon2studio

import java.nio.ByteBuffer
import java.nio.charset.StandardCharsets
import java.security.MessageDigest

/** The keys made from a session secret (joycon2_link.py's Keys). */
class Keys(secret: ByteArray) {
    val enc: ByteArray
    val mac: ByteArray

    init {
        require(secret.size >= 16) { "the session secret is too short" }
        enc = Ed25519.hmacSha256(secret, "joycon2studio encryption".toByteArray(StandardCharsets.US_ASCII))
        mac = Ed25519.hmacSha256(secret, "joycon2studio authentication".toByteArray(StandardCharsets.US_ASCII))
    }
}

/** One packet after the handshake. */
class Packet(
    val ptype: Int,
    val direction: Int,
    val session: ByteArray,
    val counter: Long,
    val payload: ByteArray,
)

/**
 * The sealed packet format of joycon2_link.py: a readable but authenticated header, an HMAC keystream for the
 * payload, and a 16-byte HMAC tag (encrypt-then-MAC).
 */
object Packets {
    private val MAGIC: ByteArray = Generated.LINK_MAGIC.toByteArray(StandardCharsets.US_ASCII)
    private const val HEADER_LEN = Generated.LINK_HEADER_LEN
    private const val TAG_LEN = Generated.LINK_TAG_LEN

    fun header(ptype: Int, direction: Int, session: ByteArray, counter: Long): ByteArray =
        ByteBuffer.allocate(HEADER_LEN)
            .put(MAGIC)
            .put(Generated.LINK_VERSION.toByte())
            .put(ptype.toByte())
            .put(direction.toByte())
            .put(session)
            .putLong(counter)
            .array()

    fun keystream(key: ByteArray, direction: Int, session: ByteArray, counter: Long, length: Int): ByteArray {
        val prefix = ByteBuffer.allocate(17).put(direction.toByte()).put(session).putLong(counter).array()
        val out = ByteArray(length)
        var filled = 0
        var block = 0
        while (filled < length) {
            val chunk = Ed25519.hmacSha256(key, prefix + blockBytes(block))
            val count = minOf(chunk.size, length - filled)
            System.arraycopy(chunk, 0, out, filled, count)
            filled += count
            block++
        }
        return out
    }

    fun seal(keys: Keys, ptype: Int, direction: Int, session: ByteArray, counter: Long, payload: ByteArray): ByteArray {
        val head = header(ptype, direction, session, counter)
        val body = xor(payload, keystream(keys.enc, direction, session, counter, payload.size))
        val tag = Ed25519.hmacSha256(keys.mac, head + body).copyOf(TAG_LEN)
        return head + body + tag
    }

    /** The packet, or null when the bytes are not a valid packet for these keys. */
    fun open(keys: Keys, data: ByteArray): Packet? {
        if (data.size < HEADER_LEN + TAG_LEN) return null
        val head = data.copyOfRange(0, HEADER_LEN)
        val body = data.copyOfRange(HEADER_LEN, data.size - TAG_LEN)
        val tag = data.copyOfRange(data.size - TAG_LEN, data.size)
        val buf = ByteBuffer.wrap(head)
        val magic = ByteArray(4)
        buf.get(magic)
        val version = buf.get().toInt() and 0xFF
        val ptype = buf.get().toInt() and 0xFF
        val direction = buf.get().toInt() and 0xFF
        val session = ByteArray(8)
        buf.get(session)
        val counter = buf.long
        if (!magic.contentEquals(MAGIC) || version != Generated.LINK_VERSION || counter == 0L) return null
        val expected = Ed25519.hmacSha256(keys.mac, head + body).copyOf(TAG_LEN)
        if (!MessageDigest.isEqual(expected, tag)) return null
        val payload = xor(body, keystream(keys.enc, direction, session, counter, body.size))
        return Packet(ptype, direction, session, counter, payload)
    }

    private fun blockBytes(n: Int): ByteArray =
        byteArrayOf((n ushr 24).toByte(), (n ushr 16).toByte(), (n ushr 8).toByte(), n.toByte())

    private fun xor(a: ByteArray, b: ByteArray): ByteArray =
        ByteArray(a.size) { (a[it].toInt() xor b[it].toInt()).toByte() }
}
