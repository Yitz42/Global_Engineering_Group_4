package org.meshlearn.phone

import android.database.Cursor
import android.database.sqlite.SQLiteCursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteProgram
import org.meshlearn.core.Db
import org.meshlearn.core.Row
import java.io.File

/** The phone's database on Android's built-in SQLite, with values bound by type (numbers stay numbers). */
class AndroidDb(file: File, schema: String) : Db {
    private val db: SQLiteDatabase = SQLiteDatabase.openOrCreateDatabase(file, null)

    init {
        executeScript(schema)
    }

    private fun bind(p: SQLiteProgram, params: Array<out Any?>) {
        params.forEachIndexed { i, v ->
            val n = i + 1
            when (v) {
                null -> p.bindNull(n)
                is Boolean -> p.bindLong(n, if (v) 1 else 0)
                is Int -> p.bindLong(n, v.toLong())
                is Long -> p.bindLong(n, v)
                is Double -> p.bindDouble(n, v)
                is Float -> p.bindDouble(n, v.toDouble())
                else -> p.bindString(n, v.toString())
            }
        }
    }

    @Synchronized
    override fun execute(sql: String, vararg params: Any?): Int {
        val head = sql.trimStart().take(16).uppercase()
        if (head.startsWith("SELECT")) {
            all(sql, *params)
            return 0
        }
        if (params.isEmpty() && (head.startsWith("CREATE") || head.startsWith("ALTER") || head.startsWith("DROP"))) {
            db.execSQL(sql)
            return 0
        }
        db.compileStatement(sql).use { st ->
            bind(st, params)
            return if (head.startsWith("INSERT")) (if (st.executeInsert() == -1L) 0 else 1) else st.executeUpdateDelete()
        }
    }

    @Synchronized
    override fun all(sql: String, vararg params: Any?): List<Row> {
        val factory = SQLiteDatabase.CursorFactory { _, driver, editTable, query ->
            bind(query, params)
            SQLiteCursor(driver, editTable, query)
        }
        db.rawQueryWithFactory(factory, sql, null, null).use { c ->
            val out = ArrayList<Row>(c.count)
            val names = c.columnNames
            while (c.moveToNext()) {
                val row = LinkedHashMap<String, Any?>()
                for (i in names.indices) {
                    row[names[i]] = when (c.getType(i)) {
                        Cursor.FIELD_TYPE_NULL -> null
                        Cursor.FIELD_TYPE_INTEGER -> c.getLong(i)
                        Cursor.FIELD_TYPE_FLOAT -> c.getDouble(i)
                        Cursor.FIELD_TYPE_BLOB -> String(c.getBlob(i), Charsets.UTF_8)
                        else -> c.getString(i)
                    }
                }
                out.add(row)
            }
            return out
        }
    }

    @Synchronized
    override fun close() = db.close()
}
