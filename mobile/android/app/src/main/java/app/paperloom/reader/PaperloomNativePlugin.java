package app.paperloom.reader;

import android.app.Activity;
import android.graphics.Color;
import android.content.Intent;
import android.net.Uri;
import android.database.Cursor;
import android.provider.DocumentsContract;
import android.provider.OpenableColumns;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import androidx.activity.result.ActivityResult;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.ActivityCallback;
import com.getcapacitor.annotation.CapacitorPlugin;
import java.io.*;
import java.net.ConnectException;
import java.net.UnknownHostException;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.util.*;
import java.util.concurrent.*;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;
import javax.net.ssl.SSLException;
import okhttp3.*;
import org.json.JSONObject;

@CapacitorPlugin(name="PaperloomNative")
public class PaperloomNativePlugin extends Plugin {
    private static final long MAX_FILE=180L*1024*1024;
    private static final int CHUNK=512*1024, MAX_TEXT=2*1024*1024;
    private static final String ALIAS="paperloom-webdav-v1";
    private final ExecutorService io=Executors.newFixedThreadPool(2);
    private final Set<Call> calls=ConcurrentHashMap.newKeySet();
    private final OkHttpClient client=new OkHttpClient.Builder().followRedirects(false).followSslRedirects(false)
        .connectTimeout(25,TimeUnit.SECONDS).readTimeout(90,TimeUnit.SECONDS).writeTimeout(90,TimeUnit.SECONDS).callTimeout(300,TimeUnit.SECONDS).build();
    private File directory;
    @Override public void load() {
        directory=new File(getContext().getCacheDir(),"paperloom-transfers");directory.mkdirs();
        File[] old=directory.listFiles();
        if(old!=null)for(File file:old)if(System.currentTimeMillis()-file.lastModified()>86400000)file.delete();
    }
    private interface Work { JSObject run() throws Exception; }
    private void background(PluginCall call,Work work) {
        io.execute(()->{try{call.resolve(work.run());}catch(Exception error){
            String message="操作未完成，请检查网络、文件权限或服务地址。";
            if(error instanceof UnknownHostException)message="无法解析 WebDAV 域名，请检查手机网络与服务地址。";
            else if(error instanceof ConnectException)message="无法连接 WebDAV 服务，请检查手机网络及服务端口是否可访问。";
            else if(error instanceof SocketTimeoutException)message="WebDAV 连接或传输超时，请检查网络后重试。";
            else if(error instanceof SSLException)message="WebDAV TLS 证书验证未通过，请检查 HTTPS 地址和证书。";
            call.reject(message,"PAPERLOOM_"+error.getClass().getSimpleName().toUpperCase(Locale.ROOT));
        }});
    }
    private File file(String token) throws IOException {
        if(token==null || !token.matches("[a-f0-9-]{36}"))throw new IOException("Invalid transfer");
        File result=new File(directory,token);
        if(!result.getCanonicalFile().getParentFile().equals(directory.getCanonicalFile()))throw new IOException("Invalid path");
        return result;
    }
    private File newFile() throws IOException {
        File result=file(UUID.randomUUID().toString());
        if(!result.createNewFile())throw new IOException("Transfer creation failed");return result;
    }
    private JSObject fileInfo(File file) { JSObject out=new JSObject();out.put("token",file.getName());out.put("size",file.length());return out; }
    @PluginMethod public void createFile(PluginCall call) {background(call,()->fileInfo(newFile()));}
    @PluginMethod public void writeChunk(PluginCall call) {background(call,()->{
        String text=call.getString("data","");if(text.length()>CHUNK*4/3+8)throw new IOException("Chunk limit");
        byte[] bytes=Base64.decode(text,Base64.NO_WRAP);File target=file(call.getString("token"));
        int offset=call.getInt("offset",-1);if(offset<0 || offset!=target.length() || offset+bytes.length>MAX_FILE)throw new IOException("Chunk order/size");
        try(FileOutputStream stream=new FileOutputStream(target,true)){stream.write(bytes);}return new JSObject();
    });}
    @PluginMethod public void readChunk(PluginCall call) {background(call,()->{
        File source=file(call.getString("token"));int offset=call.getInt("offset",-1),length=call.getInt("length",CHUNK);
        if(offset<0 || offset>source.length() || length<=0 || length>CHUNK)throw new IOException("Chunk range");
        byte[] bytes=new byte[(int)Math.min(length,source.length()-offset)];
        try(RandomAccessFile stream=new RandomAccessFile(source,"r")){stream.seek(offset);stream.readFully(bytes);}
        JSObject out=new JSObject();out.put("data",Base64.encodeToString(bytes,Base64.NO_WRAP));return out;
    });}
    @PluginMethod public void releaseFile(PluginCall call) {background(call,()->{file(call.getString("token")).delete();return new JSObject();});}
    private Request request(PluginCall call,RequestBody body) throws Exception {
        HttpUrl url=HttpUrl.get(call.getString("url",""));
        if(!url.username().isEmpty() || !url.password().isEmpty() || url.fragment()!=null || url.query()!=null)throw new IOException("Invalid endpoint");
        if(!url.isHttps() && !call.getBoolean("allowHttp",false))throw new IOException("HTTPS required");
        String method=call.getString("method","GET").toUpperCase(Locale.ROOT);
        if(!Arrays.asList("GET","HEAD","PUT","PROPFIND","MKCOL","OPTIONS").contains(method))throw new IOException("Invalid method");
        Request.Builder builder=new Request.Builder().url(url).method(method,body);
        JSObject headers=call.getObject("headers",new JSObject());Iterator<String> keys=headers.keys();
        while(keys.hasNext()) {String key=keys.next();if(Arrays.asList("content-type","if-match","if-none-match","depth").contains(key.toLowerCase(Locale.ROOT)))builder.header(key,headers.getString(key));}
        String username=call.getString("username",""),password=call.getString("password","");
        if(username.length()>500 || password.length()>2000)throw new IOException("Credential limit");
        if(!username.isEmpty() || !password.isEmpty())builder.header("Authorization",Credentials.basic(username,password,StandardCharsets.UTF_8));
        return builder.build();
    }
    private final class Connection implements AutoCloseable {
        final Call call;final Response response;
        Connection(Call call,Response response){this.call=call;this.response=response;}
        public void close(){response.close();calls.remove(call);}
    }
    private Connection perform(Request request) throws IOException {
        Call call=client.newCall(request);calls.add(call);
        try{return new Connection(call,call.execute());}catch(IOException error){calls.remove(call);throw error;}
    }
    private JSObject responseInfo(Response response) {
        JSObject out=new JSObject(),headers=new JSObject();out.put("status",response.code());
        for(String key:Arrays.asList("etag","content-length","content-type","dav","last-modified"))if(response.header(key)!=null)headers.put(key,response.header(key));
        out.put("headers",headers);return out;
    }
    private long copy(InputStream input,OutputStream output,long limit) throws IOException {
        byte[] buffer=new byte[65536];long total=0;int count;
        while((count=input.read(buffer))!=-1){total+=count;if(total>limit)throw new IOException("Size limit");output.write(buffer,0,count);}return total;
    }
    @PluginMethod public void request(PluginCall call) {background(call,()->{
        String method=call.getString("method","GET"),data=call.getString("data","");
        if(data.getBytes(StandardCharsets.UTF_8).length>MAX_TEXT)throw new IOException("Metadata limit");
        String contentType=call.getObject("headers",new JSObject()).optString("Content-Type","application/json; charset=utf-8");
        RequestBody body=Arrays.asList("PUT","PROPFIND","MKCOL").contains(method)?RequestBody.create(data,method.equals("MKCOL")?null:MediaType.get(contentType)):null;
        try(Connection connection=perform(request(call,body))){Response response=connection.response;
            JSObject out=responseInfo(response);ByteArrayOutputStream bytes=new ByteArrayOutputStream();
            if(response.body()!=null)copy(response.body().byteStream(),bytes,MAX_TEXT);
            out.put("body",bytes.toString(StandardCharsets.UTF_8.name()));return out;
        }
    });}
    @PluginMethod public void upload(PluginCall call) {background(call,()->{
        File source=file(call.getString("token"));if(source.length()>MAX_FILE)throw new IOException("Size limit");
        try(Connection connection=perform(request(call,RequestBody.create(source,MediaType.get("application/zip"))))){return responseInfo(connection.response);}
    });}
    @PluginMethod public void download(PluginCall call) {background(call,()->{
        File destination=null;
        try(Connection connection=perform(request(call,null))){Response response=connection.response;
            JSObject out=responseInfo(response);if(!response.isSuccessful())return out;
            if(response.body()==null || response.body().contentLength()>MAX_FILE)throw new IOException("Size limit");
            destination=newFile();try(FileOutputStream stream=new FileOutputStream(destination)){copy(response.body().byteStream(),stream,MAX_FILE);}
            out.put("token",destination.getName());out.put("size",destination.length());return out;
        } catch(Exception error){if(destination!=null)destination.delete();throw error;}
    });}
    @PluginMethod public void cancelRequests(PluginCall call){for(Call request:calls)request.cancel();client.dispatcher().cancelAll();call.resolve();}
    @PluginMethod public void moveToBackground(PluginCall call) {
        getActivity().runOnUiThread(()->{getActivity().moveTaskToBack(true);call.resolve();});
    }
    @PluginMethod public void setReaderBackground(PluginCall call) {
        final int color=Color.parseColor(call.getBoolean("night",false)?"#192629":"#f8f7f3");
        getActivity().runOnUiThread(()->{getActivity().getWindow().getDecorView().setBackgroundColor(color);call.resolve();});
    }
    @PluginMethod public void pickFile(PluginCall call) {
        Intent intent=new Intent(Intent.ACTION_OPEN_DOCUMENT);intent.addCategory(Intent.CATEGORY_OPENABLE);intent.setType("*/*");
        startActivityForResult(call,intent,"pickedFile");
    }
    @ActivityCallback private void pickedFile(PluginCall call,ActivityResult result) {
        if(call==null)return;
        Uri uri=result.getData()==null?null:result.getData().getData();
        if(result.getResultCode()!=Activity.RESULT_OK || uri==null){call.reject("已取消选择","CANCELLED");return;}
        background(call,()->{
            File destination=newFile();
            try(InputStream input=getContext().getContentResolver().openInputStream(uri);FileOutputStream output=new FileOutputStream(destination)) {
                if(input==null)throw new IOException("File access");copy(input,output,MAX_FILE);return fileInfo(destination);
            }catch(Exception error){destination.delete();throw error;}
        });
    }
    private String exportName(PluginCall call) {
        String name=call.getString("name","paper.folio").replaceAll("[\\\\/:*?\"<>|\\p{Cntrl}]","_");
        if(name.length()>120)name=name.substring(0,110)+".folio";
        return name.trim().isEmpty()?"paper.folio":name;
    }
    private String documentName(Uri uri) {
        try(Cursor cursor=getContext().getContentResolver().query(uri,new String[]{OpenableColumns.DISPLAY_NAME},null,null,null)) {
            if(cursor!=null&&cursor.moveToFirst())return cursor.getString(0);
        }catch(Exception ignored){}
        return null;
    }
    @PluginMethod public void exportFile(PluginCall call) {
        String name=exportName(call);
        Intent intent=new Intent(Intent.ACTION_CREATE_DOCUMENT);intent.addCategory(Intent.CATEGORY_OPENABLE);intent.setType("application/octet-stream");intent.putExtra(Intent.EXTRA_TITLE,name);
        startActivityForResult(call,intent,"exportedFile");
    }
    @ActivityCallback private void exportedFile(PluginCall call,ActivityResult result) {
        if(call==null)return;
        Uri uri=result.getData()==null?null:result.getData().getData();
        if(result.getResultCode()!=Activity.RESULT_OK || uri==null){call.reject("已取消保存","CANCELLED");return;}
        background(call,()->{
            try(InputStream input=new FileInputStream(file(call.getString("token")));OutputStream output=getContext().getContentResolver().openOutputStream(uri,"w")) {
                if(output==null)throw new IOException("File access");copy(input,output,MAX_FILE);
            }
            String desired=exportName(call),saved=documentName(uri);
            // Some document providers replace an unknown .folio suffix with .bin.
            // Rename only the document just created by this export operation.
            if(saved!=null&&!desired.equals(saved)&&DocumentsContract.isDocumentUri(getContext(),uri)) {
                try {Uri renamed=DocumentsContract.renameDocument(getContext().getContentResolver(),uri,desired);if(renamed!=null)saved=documentName(renamed);}
                catch(Exception ignored){} // The file is saved even when renaming is unsupported.
            }
            JSObject out=new JSObject();out.put("name",saved==null?"":saved);out.put("namePreserved",desired.equals(saved));return out;
        });
    }
    private SecretKey key() throws Exception {
        KeyStore store=KeyStore.getInstance("AndroidKeyStore");store.load(null);
        if(store.containsAlias(ALIAS))return (SecretKey)store.getKey(ALIAS,null);
        KeyGenerator generator=KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES,"AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(ALIAS,KeyProperties.PURPOSE_ENCRYPT|KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build());return generator.generateKey();
    }
    @PluginMethod public void saveSecret(PluginCall call){background(call,()->{
        String value=call.getString("value","");if(value.length()>8192)throw new IOException("Secret limit");
        Cipher cipher=Cipher.getInstance("AES/GCM/NoPadding");cipher.init(Cipher.ENCRYPT_MODE,key());
        String encrypted=Base64.encodeToString(cipher.getIV(),Base64.NO_WRAP)+":"+Base64.encodeToString(cipher.doFinal(value.getBytes(StandardCharsets.UTF_8)),Base64.NO_WRAP);
        if(!getContext().getSharedPreferences("paperloom-vault",0).edit().putString("webdav",encrypted).commit())throw new IOException("Vault save");return new JSObject();
    });}
    @PluginMethod public void loadSecret(PluginCall call){background(call,()->{
        String encrypted=getContext().getSharedPreferences("paperloom-vault",0).getString("webdav","");JSObject out=new JSObject();
        if(encrypted.isEmpty()){out.put("value","");return out;}
        String[] parts=encrypted.split(":",2);Cipher cipher=Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE,key(),new GCMParameterSpec(128,Base64.decode(parts[0],Base64.NO_WRAP)));
        out.put("value",new String(cipher.doFinal(Base64.decode(parts[1],Base64.NO_WRAP)),StandardCharsets.UTF_8));return out;
    });}
    @PluginMethod public void clearSecret(PluginCall call){background(call,()->{getContext().getSharedPreferences("paperloom-vault",0).edit().remove("webdav").commit();return new JSObject();});}
    @Override protected void handleOnDestroy(){client.dispatcher().cancelAll();io.shutdownNow();super.handleOnDestroy();}
}
