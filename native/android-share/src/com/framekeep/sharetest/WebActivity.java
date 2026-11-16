package com.framekeep.sharetest;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.Intent;
import android.content.ContentValues;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Bundle;
import android.os.Build;
import android.os.Environment;
import android.provider.MediaStore;
import android.util.Base64;
import android.webkit.HttpAuthHandler;
import android.webkit.WebMessage;
import android.webkit.WebMessagePort;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import java.io.File;
import java.io.FileOutputStream;
import java.io.FileInputStream;
import java.io.OutputStream;
import java.util.UUID;
import org.json.JSONObject;

public class WebActivity extends Activity {
    private WebView web;
    private TextView status;
    private volatile String trustedOrigin;
    private volatile boolean bundledUi;
    private int pageGeneration;
    private WebMessagePort port;
    private File partial;
    private FileOutputStream output;
    private long expected, received;
    private int sequence;
    private String transferAction;
    private File pendingSave;
    private static final long LIMIT = 128L * 1024 * 1024;

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        LinearLayout root = new LinearLayout(this); root.setOrientation(LinearLayout.VERTICAL); root.setPadding(0,48,0,24);
        LinearLayout bar = new LinearLayout(this);
        EditText address = new EditText(this); address.setSingleLine(); address.setHint("������ʹ�õ�ʰ֡��վ��ַ");
        address.setText(getPreferences(0).getString("site", ""));
        bar.addView(address,new LinearLayout.LayoutParams(0,-2,1));
        Button open = new Button(this); open.setText("����վ"); bar.addView(open); root.addView(bar);
        status = new TextView(this); status.setPadding(16,8,16,8); status.setText("���������վ����¼��ʰȡ��Ƶ������ʱ����Ҫȥ���ѡ��"); root.addView(status);
        web = new WebView(this); root.addView(web,new LinearLayout.LayoutParams(-1,0,1)); setContentView(root);
        web.getSettings().setJavaScriptEnabled(true); web.getSettings().setDomStorageEnabled(true);
        web.getSettings().setAllowFileAccess(false); web.getSettings().setAllowContentAccess(false);
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return !origin(request.getUrl()).equals(trustedOrigin);
            }
            @Override public void onPageStarted(WebView view,String url,Bitmap icon) { pageGeneration++;disconnect(); }
            @Override public void onPageFinished(WebView view,String url) {
                if (!origin(Uri.parse(url)).equals(trustedOrigin)) return;
                if (!bundledUi) { probeOriginalLogin(pageGeneration);return; }
                String mainPath=Uri.parse(url).getPath();
                if (!("/".equals(mainPath)||"/index.html".equals(mainPath))) return;
                disconnect();
                WebMessagePort[] pair = web.createWebMessageChannel(); port = pair[0];
                final WebMessagePort current = port;
                port.setWebMessageCallback(new WebMessagePort.WebMessageCallback() {
                    @Override public void onMessage(WebMessagePort source,WebMessage message) {
                        if (source != port || !origin(Uri.parse(web.getUrl())).equals(trustedOrigin)) return;
                        receive(message.getData(),current);
                    }
                });
                web.postWebMessage(new WebMessage("framekeep-native-v1",new WebMessagePort[]{pair[1]}),Uri.parse(trustedOrigin));
                status.setText("��������ҳ����������ʰȡ��������Ƶ���ٵ���Ƶ���½ǵķ�����ť��");
            }
            @Override public WebResourceResponse shouldInterceptRequest(WebView view,WebResourceRequest request) {
                if (!bundledUi || !"GET".equals(request.getMethod()) || !origin(request.getUrl()).equals(trustedOrigin)) return null;
                String path = request.getUrl().getPath();
                if (path == null || path.equals("/") || path.equals("/index.html")) path = "/index.html";
                if (path.startsWith("/api/") || path.contains("..") || !path.matches("/[A-Za-z0-9_./-]+")) return null;
                String mime = path.endsWith(".html") ? "text/html" : path.endsWith(".js") ? "application/javascript" : path.endsWith(".css") ? "text/css" : path.endsWith(".svg") ? "image/svg+xml" : path.endsWith(".jpg") ? "image/jpeg" : path.endsWith(".woff2") ? "font/woff2" : "application/octet-stream";
                try { return new WebResourceResponse(mime,"UTF-8",getAssets().open("web"+path)); }
                catch (Exception missing) { return null; }
            }
            @Override public void onReceivedHttpAuthRequest(WebView view,HttpAuthHandler handler,String host,String realm) {
                if (!host.equalsIgnoreCase(Uri.parse(trustedOrigin).getHost()) || (web.getUrl()!=null && !origin(Uri.parse(web.getUrl())).equals(trustedOrigin))) {handler.cancel();return;}
                LinearLayout fields = new LinearLayout(WebActivity.this); fields.setOrientation(1);
                EditText user = new EditText(WebActivity.this); user.setHint("�û���������վ��Ҫ��"); fields.addView(user);
                EditText pass = new EditText(WebActivity.this); pass.setHint("��վ����"); pass.setInputType(129); fields.addView(pass);
                new AlertDialog.Builder(WebActivity.this).setTitle("��¼���������վ").setView(fields)
                    .setPositiveButton("��¼",(d,w)->handler.proceed(user.getText().toString(),pass.getText().toString()))
                    .setNegativeButton("ȡ��",(d,w)->handler.cancel()).setOnCancelListener(d->handler.cancel()).show();
            }
        });
        open.setOnClickListener(v->{
            String value=address.getText().toString().trim(); Uri uri=Uri.parse(value);
            if (!("https".equals(uri.getScheme()) || "http".equals(uri.getScheme())) || uri.getHost()==null || uri.getUserInfo()!=null) { status.setText("��������������վ��ַ����Ҫ������Ž���ַ��");return; }
            Runnable load=()->{pageGeneration++;disconnect();bundledUi=false;trustedOrigin=origin(uri);status.setText("�������·�ԭ��վ����������룬��¼�ɹ����Զ�����������档");getPreferences(0).edit().putString("site",value).apply();web.loadUrl(value);};
            if ("http".equals(uri.getScheme())) new AlertDialog.Builder(this).setTitle("�����ַû�м���")
                .setMessage("��¼��Ϣ����Ƶ���ܱ������е������˿�������ȷ�����������εĲ�����վ������ʹ�ü�����ַ��")
                .setPositiveButton("������",(d,w)->load.run()).setNegativeButton("ȡ��",null).show();
            else load.run();
        });
    }
    private void probeOriginalLogin(int generation) {
        try(java.io.InputStream input=getAssets().open("login-probe.js")) {
            java.io.ByteArrayOutputStream source=new java.io.ByteArrayOutputStream();
            byte[] bytes=new byte[4096];int count;while((count=input.read(bytes))!=-1)source.write(bytes,0,count);
            web.evaluateJavascript(source.toString("UTF-8"),ignored->waitForLogin(generation,0));
        }catch(Exception error){status.setText("�������վ��¼���ٵ㡰����վ�����ԡ�");}
    }
    private void waitForLogin(int generation,int attempt) {
        web.postDelayed(()->{
            if(isDestroyed()||generation!=pageGeneration||bundledUi||!origin(Uri.parse(web.getUrl())).equals(trustedOrigin))return;
            web.evaluateJavascript("window.__framekeepAuthState || 'pending'",value->{
                if(isDestroyed()||generation!=pageGeneration||bundledUi)return;
                if("\"ready\"".equals(value)) {
                    bundledUi=true;status.setText("��¼��ȷ�ϣ����ڴ򿪴��������ܵĽ��桭");web.loadUrl(trustedOrigin+"/");
                }else if("\"pending\"".equals(value)&&attempt<20)waitForLogin(generation,attempt+1);
                else status.setText("����ԭ��վ����������롣�ѵ�¼��δ�л�ʱ�����ٵ㡰����վ����");
            });
        },400);
    }
    private String origin(Uri uri) {
        if(uri==null||uri.getHost()==null||uri.getScheme()==null)return "";
        int p=uri.getPort();boolean defaultPort=p==-1||("https".equals(uri.getScheme())&&p==443)||("http".equals(uri.getScheme())&&p==80);
        return uri.getScheme().toLowerCase()+"://"+uri.getHost().toLowerCase()+(defaultPort?"":":"+p);
    }
    private void receive(String raw,WebMessagePort replyPort) {
        String id="";
        try {
            if(raw==null||raw.length()>50000)throw new IllegalArgumentException();
            JSONObject message=new JSONObject(raw);id=message.getString("id");if(id.length()>40)throw new IllegalArgumentException();
            String op=message.getString("op");
            if("begin".equals(op)) {
                abortTransfer();expected=message.getLong("size");
                transferAction=message.optString("action","share");
                if(!"share".equals(transferAction)&&!"save".equals(transferAction))throw new IllegalArgumentException();
                if(expected<=0||expected>LIMIT||!"video/mp4".equals(message.getString("mime")))throw new IllegalArgumentException();
                File directory=new File(getCacheDir(),"shared");if(!directory.isDirectory()&&!directory.mkdirs())throw new IllegalStateException();
                partial=new File(directory,UUID.randomUUID().toString().replace("-","")+".mp4");
                output=new FileOutputStream(partial);received=0;sequence=0;
            } else if("chunk".equals(op)) {
                if(output==null||message.getInt("index")!=sequence)throw new IllegalArgumentException();
                String encoded=message.getString("data");if(!encoded.matches("[A-Za-z0-9+/]{1,43692}={0,2}"))throw new IllegalArgumentException();
                byte[] bytes=Base64.decode(encoded,Base64.NO_WRAP);
                if(bytes.length>32768||received+bytes.length>expected)throw new IllegalArgumentException();
                output.write(bytes);received+=bytes.length;sequence++;
            } else if("finish".equals(op)) {
                if(output==null||received!=expected)throw new IllegalArgumentException();
                output.close();output=null;
                if("save".equals(transferAction)) {
                    File completed=partial;
                    String saved=saveVideo(completed);
                    partial=null;
                    status.setText(saved);
                    replyPort.postMessage(new WebMessage(new JSONObject().put("id",id).put("ok",true).put("message",saved).toString()));
                    return;
                }
                Uri video=Uri.parse("content://com.framekeep.sharetest.video/"+partial.getName());partial=null;
                Intent send=new Intent(Intent.ACTION_SEND);send.setType("video/mp4");send.setPackage("com.tencent.mm");
                send.putExtra(Intent.EXTRA_STREAM,video);send.setClipData(ClipData.newRawUri("ʰ֡��ҳ��Ƶ",video));send.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                startActivityForResult(send,11);status.setText("��ǰ��ҳ��Ƶ�ѽ���΢�ţ�������ѡ����Ѳ�ȷ�ϡ�");
            } else if("cancel".equals(op)) abortTransfer();
            else throw new IllegalArgumentException();
            replyPort.postMessage(new WebMessage(new JSONObject().put("id",id).put("ok",true).toString()));
        } catch(Exception error) {
            abortTransfer();
            try {replyPort.postMessage(new WebMessage(new JSONObject().put("id",id).put("ok",false).put("error","Ӧ��δ�ܽ�����Ƶ�������ļ���ʽ�������Լ�΢���Ƿ���á�").toString()));}catch(Exception ignored){}
        }
    }
    private String saveVideo(File source) throws Exception {
        if(Build.VERSION.SDK_INT<29) {
            if(pendingSave!=null)throw new IllegalStateException("���������һ�α��档");
            Intent create=new Intent(Intent.ACTION_CREATE_DOCUMENT);create.addCategory(Intent.CATEGORY_OPENABLE);
            create.setType("video/mp4");create.putExtra(Intent.EXTRA_TITLE,"ʰ֡-"+System.currentTimeMillis()+".mp4");
            pendingSave=source;
            try {startActivityForResult(create,12);}catch(Exception error){pendingSave=null;throw error;}
            return "��ѡ���ֻ��ϵı���λ�ã�ȷ�Ϻ�Żᱣ����Ƶ��";
        }
        ContentValues values=new ContentValues();
        values.put(MediaStore.Video.Media.DISPLAY_NAME,"ʰ֡-"+System.currentTimeMillis()+".mp4");
        values.put(MediaStore.Video.Media.MIME_TYPE,"video/mp4");
        values.put(MediaStore.Video.Media.RELATIVE_PATH,Environment.DIRECTORY_MOVIES+"/Framekeep");
        values.put(MediaStore.Video.Media.IS_PENDING,1);
        Uri destination=getContentResolver().insert(MediaStore.Video.Media.EXTERNAL_CONTENT_URI,values);
        if(destination==null)throw new IllegalStateException();
        try {
            copyVideo(source,destination);
            values.clear();values.put(MediaStore.Video.Media.IS_PENDING,0);
            if(getContentResolver().update(destination,values,null,null)!=1)throw new IllegalStateException();
        }catch(Exception error){getContentResolver().delete(destination,null,null);throw error;}
        source.delete();return "��Ƶ�ѱ��浽�ֻ���ᣬ���� Movies/Framekeep �ļ����ҵ���";
    }
    private void copyVideo(File source,Uri destination) throws Exception {
        try(FileInputStream input=new FileInputStream(source);OutputStream target=getContentResolver().openOutputStream(destination)) {
            if(target==null)throw new IllegalStateException();
            byte[] bytes=new byte[32768];int count;while((count=input.read(bytes))!=-1)target.write(bytes,0,count);
        }
    }
    @Override protected void onActivityResult(int request,int result,Intent data) {
        super.onActivityResult(request,result,data);
        if(request!=12||pendingSave==null)return;
        File source=pendingSave;pendingSave=null;
        try {
            if(result!=RESULT_OK||data==null||data.getData()==null){status.setText("��ȡ��������Ƶ��");return;}
            copyVideo(source,data.getData());status.setText("��Ƶ�ѱ��浽��ѡ���λ�á�");
        }catch(Exception error){status.setText("��Ƶ����ʧ�ܣ������ֻ�ʣ��ռ䲢���ԡ�");}
        finally{source.delete();}
    }
    private void abortTransfer(){try{if(output!=null)output.close();}catch(Exception ignored){}output=null;if(partial!=null)partial.delete();partial=null;}
    private void disconnect(){abortTransfer();if(port!=null)port.close();port=null;}
    @Override public void onBackPressed(){if(web.canGoBack())web.goBack();else super.onBackPressed();}
    @Override protected void onDestroy(){disconnect();if(pendingSave!=null)pendingSave.delete();web.destroy();super.onDestroy();}
}
