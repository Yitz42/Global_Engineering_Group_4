package org.meshlearn.core

import java.io.File

/**
 * Moves the MeshLearn folder between the memory card and the phone's own storage.
 *
 * Students' saved work, the radio outbox and the lessons always move: losing them would lose
 * work that has not reached the teacher. Books (the library folder) move only if asked, since an
 * encyclopedia can be several GB. Nothing at the old place is changed until every file has been
 * copied; then the databases there are renamed (kept as a backup, never loaded again) and moved
 * books are deleted to free the space. Run it only while MeshLearn is stopped.
 */
object StorageMove {
    const val LIBRARY = "library"
    const val BACKUP_SUFFIX = ".before-move"

    class NotEnoughRoom(val needed: Long, val free: Long) : Exception("not enough room")

    /** Bytes in a file or folder. */
    fun sizeOf(f: File): Long = if (f.isFile) f.length() else (f.listFiles() ?: emptyArray()).sumOf { sizeOf(it) }

    /** What moves: everything except the books, plus the books if asked. Old backups stay behind. */
    fun items(from: File, books: Boolean): List<File> = (from.listFiles() ?: emptyArray())
        .filter { !it.name.endsWith(BACKUP_SUFFIX) && (books || it.name != LIBRARY) }
        .sortedBy { it.name }

    fun bytesToMove(from: File, books: Boolean): Long = items(from, books).sumOf { sizeOf(it) }

    /**
     * Copy, then tidy the old place. [freeAtTarget] is the room left where the files go.
     * Throws [NotEnoughRoom] before copying anything if they will not fit, or the copy's error;
     * either way the old place is left exactly as it was and is still the one to use.
     */
    fun move(from: File, to: File, books: Boolean, freeAtTarget: Long, progress: (String) -> Unit = {}) {
        val moving = items(from, books)
        val needed = moving.sumOf { sizeOf(it) }
        if (needed > freeAtTarget) throw NotEnoughRoom(needed, freeAtTarget)
        to.mkdirs()
        // File by file, merging into what is already there (books copied onto the phone by cable stay).
        val written = ArrayList<File>()
        try {
            for (item in moving) {
                progress(item.name)
                val files = if (item.isFile) listOf(item) else item.walkTopDown().filter { it.isFile }.toList()
                for (f in files) {
                    val dest = File(to, f.relativeTo(from).path)
                    dest.parentFile?.mkdirs()
                    f.copyTo(dest, overwrite = true)
                    written.add(dest)
                    if (dest.length() != f.length()) throw IllegalStateException("copy of ${f.name} is incomplete")
                }
            }
        } catch (e: Exception) {
            written.forEach { it.delete() }
            throw e
        }
        // Everything is safely at the new place. Keep the old databases as a backup that is never
        // loaded again (so a later move back cannot bring old work back to life), free the rest.
        for (item in moving) {
            if (item.isFile && (item.name.endsWith(".db") || item.name.contains(".db-"))) {
                val backup = File(from, item.name + BACKUP_SUFFIX)
                backup.delete()
                if (!item.renameTo(backup)) item.delete()
            } else {
                item.deleteRecursively()
            }
        }
    }
}
