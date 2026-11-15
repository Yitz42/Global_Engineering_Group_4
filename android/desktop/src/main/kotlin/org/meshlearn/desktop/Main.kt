package org.meshlearn.desktop

import org.meshlearn.core.JdbcDb
import org.meshlearn.core.Messenger
import org.meshlearn.core.Phone
import org.meshlearn.core.UdpSimTransport
import java.io.File
import kotlin.system.exitProcess

/**
 * Run the Kotlin phone on a computer with the simulated radio, to test it against the Python
 * hub without hardware. Same options as `python -m meshlearn phone` (simulator only):
 *
 *   python -m meshlearn hub --sim-peer phone-1=127.0.0.1:47002
 *   gradlew :desktop:run --args="--data phone-data --hub hub --sim-peer hub=127.0.0.1:47001"
 */
fun main(argv: Array<String>) {
    val opts = HashMap<String, String>()
    val peers = ArrayList<String>()
    var i = 0
    while (i < argv.size) {
        val key = argv[i].removePrefix("--")
        val value = argv.getOrNull(i + 1) ?: usage("missing value for --$key")
        if (key == "sim-peer") peers.add(value) else opts[key] = value
        i += 2
    }
    val data = File(opts["data"] ?: usage("--data is required"))
    val hub = opts["hub"] ?: usage("--hub is required")
    val port = (opts["port"] ?: "8080").toInt()
    val transport = UdpSimTransport(opts["sim-node"] ?: "phone-1", (opts["sim-port"] ?: "47002").toInt(),
        UdpSimTransport.parsePeers(peers), dropRate = (opts["sim-loss"] ?: "0").toDouble())
    val phone = Phone(
        data, hub, opts["name"] ?: "Student phone",
        makeDb = { f, schema -> JdbcDb(f.path, schema) },
        messengerFactory = { handler -> Messenger(transport, JdbcDb(File(data, "outbox.db").path, ""), handler, retryS = 3.0) },
        kiwixUrl = opts["kiwix-url"] ?: "",
        batchS = (opts["batch-seconds"] ?: "120").toDouble(),
        adminPin = opts["admin-pin"] ?: "",
    )
    phone.start()
    phone.sayHello()
    val server = phone.app.server(opts["host"] ?: "0.0.0.0", port)
    server.start(5000, false)
    println("Student site on http://localhost:$port  (radio node: ${phone.nodeId})")
    Runtime.getRuntime().addShutdownHook(Thread { server.stop(); phone.stop() })
    Thread.currentThread().join()
}

private fun usage(msg: String): Nothing {
    System.err.println("$msg\nusage: --data DIR --hub NODE [--name NAME] [--port 8080] [--sim-node phone-1] " +
        "[--sim-port 47002] [--sim-peer hub=127.0.0.1:47001] [--batch-seconds 120] [--admin-pin PIN] [--kiwix-url URL]")
    exitProcess(2)
}
