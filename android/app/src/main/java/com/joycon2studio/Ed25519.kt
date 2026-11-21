package com.joycon2studio

import java.math.BigInteger
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/** A point of the Ed25519 group in extended coordinates (x, y, z, t), as joycon2_link.py keeps them. */
data class Point(val x: BigInteger, val y: BigInteger, val z: BigInteger, val t: BigInteger)

/**
 * The group arithmetic and the SPAKE2 helpers of joycon2_link.py, on BigInteger. Like the desktop code it is not
 * constant-time; the phone only runs it on its own secrets during a pairing.
 */
object Ed25519 {
    private val TWO: BigInteger = BigInteger.valueOf(2)
    private val THREE: BigInteger = BigInteger.valueOf(3)
    private val FOUR: BigInteger = BigInteger.valueOf(4)
    private val FIVE: BigInteger = BigInteger.valueOf(5)
    private val SEVEN: BigInteger = BigInteger.valueOf(7)
    private val EIGHT: BigInteger = BigInteger.valueOf(8)
    private val NINETEEN: BigInteger = BigInteger.valueOf(19)
    private val RNG = SecureRandom()
    private val ASCII = StandardCharsets.US_ASCII

    val P: BigInteger = TWO.pow(255).subtract(NINETEEN)
    val L: BigInteger = TWO.pow(252).add(BigInteger("27742317777372353535851937790883648493"))
    val D: BigInteger = BigInteger.valueOf(-121665)
        .multiply(BigInteger.valueOf(121666).modPow(P.subtract(TWO), P))
        .mod(P)
    private val SQRT_M1: BigInteger = TWO.modPow(P.subtract(BigInteger.ONE).divide(FOUR), P)
    val IDENTITY = Point(BigInteger.ZERO, BigInteger.ONE, BigInteger.ONE, BigInteger.ZERO)

    private val BY: BigInteger = FOUR.multiply(FIVE.modPow(P.subtract(TWO), P)).mod(P)
    private val BX_RAW: BigInteger = recoverX(
        (BY * BY - BigInteger.ONE).modP(),
        (D * BY * BY + BigInteger.ONE).modP(),
    ) ?: error("the base point cannot be recovered")
    private val BX: BigInteger = if (BX_RAW.testBit(0)) P.subtract(BX_RAW) else BX_RAW

    /** The standard base point. */
    val B: Point = fromAffine(BX, BY)

    /** Two points nobody knows the relation between; the pairing needs them. */
    val M: Point = hashToPoint("joycon2studio spake2 M".toByteArray(ASCII))
    val N: Point = hashToPoint("joycon2studio spake2 N".toByteArray(ASCII))

    private fun BigInteger.modP(): BigInteger = mod(P)

    fun add(p1: Point, p2: Point): Point {
        val a = ((p1.y - p1.x) * (p2.y - p2.x)).modP()
        val b = ((p1.y + p1.x) * (p2.y + p2.x)).modP()
        val c = (p1.t * TWO * D * p2.t).modP()
        val d = (p1.z * TWO * p2.z).modP()
        val e = (b - a).modP()
        val f = (d - c).modP()
        val g = (d + c).modP()
        val h = (b + a).modP()
        return Point((e * f).modP(), (g * h).modP(), (f * g).modP(), (e * h).modP())
    }

    /** k * point, without reducing k (the subgroup check needs k = L). */
    fun mul(k: BigInteger, point: Point): Point {
        var result = IDENTITY
        var addend = point
        var n = k
        while (n.signum() > 0) {
            if (n.testBit(0)) result = add(result, addend)
            addend = add(addend, addend)
            n = n.shiftRight(1)
        }
        return result
    }

    fun neg(point: Point): Point =
        Point(point.x.negate().modP(), point.y, point.z, point.t.negate().modP())

    fun equal(p1: Point, p2: Point): Boolean =
        (p1.x * p2.z - p2.x * p1.z).modP().signum() == 0 && (p1.y * p2.z - p2.y * p1.z).modP().signum() == 0

    fun affine(point: Point): Pair<BigInteger, BigInteger> {
        val inv = point.z.modPow(P.subtract(TWO), P)
        return Pair((point.x * inv).modP(), (point.y * inv).modP())
    }

    fun fromAffine(x: BigInteger, y: BigInteger): Point =
        Point(x.modP(), y.modP(), BigInteger.ONE, (x * y).modP())

    private fun onCurve(x: BigInteger, y: BigInteger): Boolean {
        val xx = x * x
        val yy = y * y
        return (yy - xx - BigInteger.ONE - D * xx * yy).modP().signum() == 0
    }

    /** An x with v * x^2 == u (mod P), or null (the RFC 8032 method). */
    private fun recoverX(u: BigInteger, v: BigInteger): BigInteger? {
        val exponent = P.subtract(FIVE).divide(EIGHT)
        val v3 = v.modPow(THREE, P)
        val v7 = v.modPow(SEVEN, P)
        val x = (u * v3 * (u * v7).modP().modPow(exponent, P)).modP()
        if ((v * x * x).modP() == u.modP()) return x
        if ((v * x * x).modP() == u.negate().modP()) return (x * SQRT_M1).modP()
        return null
    }

    private fun hashToPoint(label: ByteArray): Point {
        var counter = 0
        while (true) {
            val digest = sha512(label + intBE(counter))
            val y = littleEndian(digest.copyOfRange(0, 32)).modP()
            val u = (y * y - BigInteger.ONE).modP()
            val v = (D * y * y + BigInteger.ONE).modP()
            val x = if (v.signum() != 0) recoverX(u, v) else null
            if (x != null) {
                val point = mul(EIGHT, fromAffine(x, y))
                if (!equal(point, IDENTITY)) return point
            }
            counter++
        }
    }

    fun encodePoint(point: Point): ByteArray {
        val (x, y) = affine(point)
        return toFixedBE(x, 32) + toFixedBE(y, 32)
    }

    /** The point for 64 bytes from the other end, or null if it is not a valid group element. */
    fun decodePoint(data: ByteArray): Point? {
        if (data.size != 64) return null
        val x = BigInteger(1, data.copyOfRange(0, 32))
        val y = BigInteger(1, data.copyOfRange(32, 64))
        if (x >= P || y >= P || (x.signum() == 0 && y == BigInteger.ONE) || !onCurve(x, y)) return null
        val point = fromAffine(x, y)
        if (!equal(mul(L, point), IDENTITY)) return null
        return point
    }

    /** A uniform value from 1 to L-1, as secrets.randbelow(L - 1) + 1 on the PC. */
    fun randomScalar(): BigInteger {
        val bound = L.subtract(BigInteger.ONE)
        while (true) {
            val candidate = BigInteger(bound.bitLength(), RNG)
            if (candidate < bound) return candidate.add(BigInteger.ONE)
        }
    }

    /** The six digits of a pairing code (spaces and dashes are ignored). Throws IllegalArgumentException. */
    fun normalizePin(text: String): String {
        val digits = text.filterNot { it.isWhitespace() }.replace("-", "")
        require(digits.length == 6 && digits.all { it in '0'..'9' }) {
            "The pairing code is six digits, for example 042917."
        }
        return digits
    }

    fun pinScalar(pin: String): BigInteger =
        littleEndian(sha512("joycon2studio pin".toByteArray(ASCII) + pin.toByteArray(ASCII))).mod(L)

    fun transcript(xBytes: ByteArray, yBytes: ByteArray, zBytes: ByteArray, w: BigInteger): ByteArray =
        sha256("joycon2studio spake2 v1".toByteArray(ASCII) + xBytes + yBytes + zBytes + toFixedLE(w, 32))

    fun confirm(k: ByteArray, role: String, xBytes: ByteArray, yBytes: ByteArray): ByteArray {
        val key = hmacSha256(k, "joycon2studio confirm".toByteArray(ASCII))
        return hmacSha256(key, role.toByteArray(ASCII) + xBytes + yBytes)
    }

    fun sessionSecret(k: ByteArray): ByteArray = hmacSha256(k, "joycon2studio session".toByteArray(ASCII))

    fun sessionId(k: ByteArray): ByteArray = hmacSha256(k, "joycon2studio session id".toByteArray(ASCII)).copyOf(8)

    fun hmacSha256(key: ByteArray, data: ByteArray): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key, "HmacSHA256"))
        return mac.doFinal(data)
    }

    fun sha256(data: ByteArray): ByteArray = MessageDigest.getInstance("SHA-256").digest(data)

    fun sha512(data: ByteArray): ByteArray = MessageDigest.getInstance("SHA-512").digest(data)

    fun littleEndian(bytes: ByteArray): BigInteger = BigInteger(1, bytes.reversedArray())

    fun toFixedBE(value: BigInteger, size: Int): ByteArray {
        val raw = value.toByteArray()
        val trimmed = if (raw.size == size + 1 && raw[0] == 0.toByte()) raw.copyOfRange(1, raw.size) else raw
        require(trimmed.size <= size) { "value does not fit in $size bytes" }
        val out = ByteArray(size)
        System.arraycopy(trimmed, 0, out, size - trimmed.size, trimmed.size)
        return out
    }

    fun toFixedLE(value: BigInteger, size: Int): ByteArray = toFixedBE(value, size).reversedArray()

    private fun intBE(n: Int): ByteArray =
        byteArrayOf((n ushr 24).toByte(), (n ushr 16).toByte(), (n ushr 8).toByte(), n.toByte())
}
