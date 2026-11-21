package com.joycon2studio

import java.math.BigInteger
import java.nio.charset.StandardCharsets
import java.security.MessageDigest

/**
 * The phone's end of the Deck link: the PC side of joycon2_link.py's PcLink, as a state machine with no network.
 * Each call returns the packets to send, and LinkTransport sends them to the Deck. Times are in seconds.
 */
class LinkClient(
    private val randomScalar: () -> BigInteger,
    private val reportProvider: () -> ByteArray,
    private val listener: Listener,
) {
    interface Listener {
        fun onState(state: State)
        fun onMessage(text: String)
        fun onRumble(small: Int, large: Int)
    }

    enum class State { OFF, SEARCHING, CONFIRMING, CONNECTED }

    @Volatile
    var state: State = State.OFF
        private set

    private val hsMagic = Generated.LINK_HS_MAGIC.toByteArray(StandardCharsets.US_ASCII)
    private var deckIp = ""
    private var w: BigInteger? = null
    private var want = false
    private var x = BigInteger.ZERO
    private var xBytes = ByteArray(0)
    private var hsConfirm = ByteArray(0)
    private var keys: Keys? = null
    private var sid = ByteArray(0)
    private var counter = 0L
    private var deckCounter = 0L
    private var lastDeck = 0.0
    private var lastHs = 0.0
    private var lastState = 0.0
    private var mismatchTold = false

    /** Checks the Deck's address and the pairing code. Throws IllegalArgumentException for a bad code. */
    @Synchronized
    fun configure(deckIp: String, pinText: String) {
        val digits = Ed25519.normalizePin(pinText)
        this.deckIp = deckIp.trim()
        this.w = Ed25519.pinScalar(digits)
    }

    @Synchronized
    fun start(now: Double) {
        check(w != null && deckIp.isNotEmpty()) { "enter the Deck's address and its pairing code" }
        if (state == State.OFF) {
            newAttempt()
            setState(State.SEARCHING, "Searching for the Deck at $deckIp.")
        }
        want = true
    }

    /** True while the link should keep running. */
    @Synchronized
    fun wanted(): Boolean = want || state != State.OFF

    /** The packets that tell the Deck this phone is disconnecting. */
    @Synchronized
    fun disconnect(): List<ByteArray> {
        val out = ArrayList<ByteArray>()
        if (keys != null) out.add(send(Generated.LINK_BYE, byteArrayOf(1)))
        want = false
        keys = null
        setState(State.OFF, "Disconnected from the Deck.")
        return out
    }

    @Synchronized
    fun tick(now: Double): List<ByteArray> {
        val out = ArrayList<ByteArray>()
        if (w == null || !want) return out
        if ((state == State.CONFIRMING || state == State.CONNECTED) &&
            now - lastDeck > Generated.LINK_TIMEOUT
        ) {
            newAttempt()
            setState(State.SEARCHING, "Lost the Deck. Searching again.")
        }
        when (state) {
            State.SEARCHING -> if (now - lastHs >= Generated.LINK_HS_EVERY) {
                lastHs = now
                out.add(hsHeader(Generated.LINK_HS_X) + xBytes)
            }
            State.CONFIRMING -> if (now - lastHs >= Generated.LINK_HS_EVERY) {
                lastHs = now
                out.add(hsConfirm)
            }
            State.CONNECTED -> if (now - lastState >= Generated.LINK_STATE_EVERY) {
                lastState = now
                out.add(send(Generated.LINK_STATE, reportProvider()))
            }
            State.OFF -> Unit
        }
        return out
    }

    /** A packet arrived from fromIp. Returns the packets to send back. */
    @Synchronized
    fun onDatagram(data: ByteArray, fromIp: String, now: Double): List<ByteArray> {
        if (fromIp != deckIp || w == null) return emptyList()
        if (data.size >= 4 && data.copyOfRange(0, 4).contentEquals(hsMagic)) {
            return handleHandshake(data, now)
        }
        val current = keys ?: return emptyList()
        val pkt = Packets.open(current, data) ?: return emptyList()
        if (pkt.direction != Generated.LINK_DECK_TO_PC ||
            !pkt.session.contentEquals(sid) ||
            pkt.counter <= deckCounter
        ) {
            return emptyList()
        }
        deckCounter = pkt.counter
        lastDeck = now
        if (state == State.CONFIRMING) {
            setState(State.CONNECTED, "Connected to the Deck. This phone sends the Joy-Cons to it.")
        }
        if (pkt.ptype == Generated.LINK_RUMBLE && pkt.payload.size >= 2) {
            listener.onRumble(pkt.payload[0].toInt() and 0xFF, pkt.payload[1].toInt() and 0xFF)
        } else if (pkt.ptype == Generated.LINK_BYE) {
            want = false
            keys = null
            setState(State.OFF, "The Deck disconnected.")
        }
        return emptyList()
    }

    private fun handleHandshake(data: ByteArray, now: Double): List<ByteArray> {
        if (state != State.SEARCHING || data.size != Generated.LINK_HS_HEADER_LEN + 96) return emptyList()
        val version = data[4].toInt() and 0xFF
        val mtype = data[5].toInt() and 0xFF
        if (version != Generated.LINK_VERSION || mtype != Generated.LINK_HS_Y) return emptyList()
        val yBytes = data.copyOfRange(6, 70)
        val confirmDeck = data.copyOfRange(70, 102)
        val yPoint = Ed25519.decodePoint(yBytes) ?: return emptyList()
        val wValue = w ?: return emptyList()
        val z = Ed25519.mul(x, Ed25519.add(yPoint, Ed25519.neg(Ed25519.mul(wValue, Ed25519.N))))
        val k = Ed25519.transcript(xBytes, yBytes, Ed25519.encodePoint(z), wValue)
        val expected = Ed25519.confirm(k, "deck", xBytes, yBytes)
        if (!MessageDigest.isEqual(expected, confirmDeck)) {
            if (!mismatchTold) {
                mismatchTold = true
                listener.onMessage("The pairing code does not match the Deck's. Check the six digits.")
            }
            return emptyList()
        }
        sid = Ed25519.sessionId(k)
        keys = Keys(Ed25519.sessionSecret(k))
        counter = 0
        deckCounter = 0
        lastDeck = now
        hsConfirm = hsHeader(Generated.LINK_HS_CONFIRM) + Ed25519.confirm(k, "pc", xBytes, yBytes)
        lastHs = now
        setState(State.CONFIRMING, "The Deck accepted the code. Finishing the connection.")
        return listOf(hsConfirm)
    }

    private fun newAttempt() {
        val wValue = w ?: return
        mismatchTold = false
        x = randomScalar()
        xBytes = Ed25519.encodePoint(Ed25519.add(Ed25519.mul(x, Ed25519.B), Ed25519.mul(wValue, Ed25519.M)))
        keys = null
        sid = ByteArray(0)
        counter = 0
        deckCounter = 0
        lastHs = 0.0
    }

    private fun hsHeader(type: Int): ByteArray =
        hsMagic + byteArrayOf(Generated.LINK_VERSION.toByte(), type.toByte())

    private fun send(ptype: Int, payload: ByteArray): ByteArray {
        val current = checkNotNull(keys) { "no session keys yet" }
        counter += 1
        return Packets.seal(current, ptype, Generated.LINK_PC_TO_DECK, sid, counter, payload)
    }

    private fun setState(newState: State, message: String) {
        val changed = newState != state
        state = newState
        if (changed) listener.onState(newState)
        if (message.isNotEmpty()) listener.onMessage(message)
    }
}
