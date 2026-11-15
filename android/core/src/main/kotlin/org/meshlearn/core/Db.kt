package org.meshlearn.core

import java.sql.Connection
import java.sql.DriverManager
import java.sql.Types

typealias Row = Map<String, Any?>

/**
 * The few SQLite calls the phone needs, like the Python Database wrapper. Values come back as
 * Long, Double, String or null. Android uses its built-in SQLite (AndroidDb in the app);
 * the desktop runner and the tests use [JdbcDb].
 *
 * Statements stay within what Android 7's SQLite (3.9) understands: no UPSERT.
 */
interface Db {
    /** Run one statement (or a schema of several separated by ';'); returns rows changed. */
    fun execute(sql: String, vararg params: Any?): Int
    fun all(sql: String, vararg params: Any?): List<Row>
    fun one(sql: String, vararg params: Any?): Row? = all(sql, *params).firstOrNull()
    fun executeScript(script: String) {
        script.split(';').map { it.trim() }.filter { it.isNotEmpty() }.forEach { execute(it) }
    }
    fun close() {}
}

fun Row.str(key: String): String? = this[key]?.let { if (it is Double) Json.pyFloatRepr(it) else it.toString() }
fun Row.long(key: String): Long? = (this[key] as? Number)?.toLong()
fun Row.double(key: String): Double? = (this[key] as? Number)?.toDouble()

/** SQLite through JDBC (org.xerial:sqlite-jdbc), for running the phone on a computer. */
class JdbcDb(path: String, schema: String) : Db {
    private val conn: Connection = DriverManager.getConnection("jdbc:sqlite:$path")

    init {
        executeScript(schema)
    }

    @Synchronized
    override fun execute(sql: String, vararg params: Any?): Int {
        conn.prepareStatement(sql).use { st ->
            bind(st, params)
            return if (st.execute()) 0 else st.updateCount.coerceAtLeast(0)
        }
    }

    @Synchronized
    override fun all(sql: String, vararg params: Any?): List<Row> {
        conn.prepareStatement(sql).use { st ->
            bind(st, params)
            st.executeQuery().use { rs ->
                val meta = rs.metaData
                val out = ArrayList<Row>()
                while (rs.next()) {
                    val row = LinkedHashMap<String, Any?>()
                    for (i in 1..meta.columnCount) {
                        val v = rs.getObject(i)
                        row[meta.getColumnLabel(i)] = when (v) {
                            null -> null
                            is Int -> v.toLong()
                            is Long, is Double, is String -> v
                            is Float -> v.toDouble()
                            is Number -> if (meta.getColumnType(i) == Types.REAL) v.toDouble() else v.toLong()
                            else -> v.toString()
                        }
                    }
                    out.add(row)
                }
                return out
            }
        }
    }

    private fun bind(st: java.sql.PreparedStatement, params: Array<out Any?>) {
        params.forEachIndexed { i, p ->
            when (p) {
                null -> st.setNull(i + 1, Types.NULL)
                is Boolean -> st.setLong(i + 1, if (p) 1 else 0)
                is Int -> st.setLong(i + 1, p.toLong())
                is Long -> st.setLong(i + 1, p)
                is Double -> st.setDouble(i + 1, p)
                is Float -> st.setDouble(i + 1, p.toDouble())
                else -> st.setString(i + 1, p.toString())
            }
        }
    }

    @Synchronized
    override fun close() = conn.close()
}
