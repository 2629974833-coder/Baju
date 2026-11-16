package com.framekeep.sharetest;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;
import java.io.File;
import java.io.FileNotFoundException;

public class VideoProvider extends ContentProvider {
    @Override public boolean onCreate() { return true; }
    private File resolve(Uri uri) throws FileNotFoundException {
        String path = uri.getPath();
        if (!"com.framekeep.sharetest.video".equals(uri.getAuthority()) || path == null || !path.matches("/[a-f0-9]{32}\\.mp4")) throw new FileNotFoundException();
        File file = new File(new File(getContext().getCacheDir(), "shared"), path.substring(1));
        if (!file.isFile()) throw new FileNotFoundException();
        return file;
    }
    @Override public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        if (!"r".equals(mode)) throw new FileNotFoundException("Read only");
        return ParcelFileDescriptor.open(resolve(uri), ParcelFileDescriptor.MODE_READ_ONLY);
    }
    @Override public String getType(Uri uri) { return "video/mp4"; }
    @Override public Cursor query(Uri uri, String[] projection, String selection, String[] args, String order) {
        try {
            File file = resolve(uri);
            MatrixCursor cursor = new MatrixCursor(new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE});
            cursor.addRow(new Object[]{"拾帧分享视频.mp4", file.length()});
            return cursor;
        } catch (FileNotFoundException error) { return null; }
    }
    @Override public Uri insert(Uri uri, ContentValues values) { throw new UnsupportedOperationException(); }
    @Override public int delete(Uri uri, String selection, String[] args) { throw new UnsupportedOperationException(); }
    @Override public int update(Uri uri, ContentValues values, String selection, String[] args) { throw new UnsupportedOperationException(); }
}
