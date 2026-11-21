package com.joycon2studio

import java.io.IOException
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.SocketTimeoutException

/** Runs a LinkClient over UDP on its own thread: it receives from the Deck and sends what the client returns. */
class LinkTransport(
    private val client: LinkClient,
    private val port: Int,
    private val onError: (String) -> Unit,
) {
    /** The client's state, for the screen. */
    val state: LinkClient.State get() = client.state

    // Guarded by this. While socket is set, one link thread runs. Only that thread sends on the socket or closes it.
    private var socket: DatagramSocket? = null
    private var target: InetAddress? = null
    private val outbox = ArrayList<ByteArray>()

    /**
     * Starts pairing with the Deck at deckIp. Throws IllegalArgumentException for a bad pairing code, and IOException
     * for an address or socket that cannot be used. deckIp must be a plain IPv4 address (see isPlainIpv4), so that
     * no name lookup runs on the calling thread.
     */
    @Synchronized
    fun connect(deckIp: String, pinText: String) {
        client.configure(deckIp, pinText)
        val address = InetAddress.getByName(deckIp.trim())
        if (socket != null) {
            // The link thread is still running and will go on; from now on it sends to the new address
            target = address
            client.start(now())
            return
        }
        val s = DatagramSocket()
        s.soTimeout = 5
        try {
            client.start(now())
        } catch (e: RuntimeException) {
            s.close()
            throw e
        }
        socket = s
        target = address
        Thread({ run(s) }, "joycon2-link").apply { isDaemon = true }.start()
    }

    /** Tells the Deck this phone is disconnecting. The link thread sends the BYE, then stops and closes the socket. */
    @Synchronized
    fun disconnect() {
        val packets = client.disconnect()
        if (socket != null) outbox.addAll(packets)
    }

    private fun run(s: DatagramSocket) {
        val buffer = ByteArray(512)
        try {
            while (true) {
                val turn = nextTurn(s)
                sendAll(s, turn.address, turn.packets)
                if (!turn.running) break
                try {
                    val packet = DatagramPacket(buffer, buffer.size)
                    s.receive(packet)
                    val data = packet.data.copyOf(packet.length)
                    val from = packet.address?.hostAddress ?: ""
                    sendAll(s, turn.address, client.onDatagram(data, from, now()))
                } catch (e: SocketTimeoutException) {
                    // nothing arrived in time; the client still gets its turn below
                } catch (e: IOException) {
                    // an unreachable Deck shows up this way on some networks; keep trying
                }
                sendAll(s, turn.address, client.tick(now()))
            }
        } catch (e: RuntimeException) {
            onError("The link stopped: ${e.message}")
        } finally {
            synchronized(this) {
                if (socket === s) {
                    socket = null
                    target = null
                    outbox.clear()
                }
            }
            s.close()
        }
    }

    /**
     * Decides, under the same lock as connect() and disconnect(), whether this thread goes on, and hands over what
     * it must send now. When it stops, it clears socket in the same step, so connect() starts a new thread.
     */
    private fun nextTurn(s: DatagramSocket): Turn = synchronized(this) {
        val packets = ArrayList(outbox)
        outbox.clear()
        val address = target
        val running = socket === s && address != null && client.wanted()
        if (!running && socket === s) {
            socket = null
            target = null
        }
        Turn(address, packets, running)
    }

    private fun sendAll(s: DatagramSocket, address: InetAddress?, packets: List<ByteArray>) {
        if (address == null) return
        for (p in packets) {
            try {
                s.send(DatagramPacket(p, p.size, address, port))
            } catch (e: IOException) {
                // the Deck is not reachable right now; the client keeps trying
            }
        }
    }

    private fun now(): Double = System.nanoTime() / 1_000_000_000.0

    private class Turn(val address: InetAddress?, val packets: List<ByteArray>, val running: Boolean)
}

/** True for four decimal numbers from 0 to 255 with no leading zeros, such as 192.168.1.20. */
fun isPlainIpv4(text: String): Boolean {
    val parts = text.split('.')
    return parts.size == 4 && parts.all { part ->
        part.isNotEmpty() && part.length <= 3 && part.all { it in '0'..'9' } &&
            (part == "0" || part[0] != '0') && part.toInt() <= 255
    }
}
