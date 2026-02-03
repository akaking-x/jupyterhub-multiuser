#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
S3 Backup Manager
Handles S3 operations, workspace browsing, and file transfers
"""

import os
import io
import uuid
import mimetypes
import threading
import zipfile
from datetime import datetime

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

# In-memory transfer task tracking
_tasks = {}
_tasks_lock = threading.Lock()

WORKSPACE_ROOT = '/home'


def get_s3_config(db, username):
    """Get S3 config for user: personal first, then system fallback with user prefix"""
    # Check personal config
    user_cfg = db.s3_user_config.find_one({'username': username})
    if user_cfg and user_cfg.get('endpoint_url'):
        return {
            'endpoint_url': user_cfg['endpoint_url'],
            'access_key': user_cfg['access_key'],
            'secret_key': user_cfg['secret_key'],
            'region': user_cfg.get('region', ''),
            'bucket_name': user_cfg['bucket_name'],
            'prefix': user_cfg.get('prefix', ''),
            'source': 'personal',
        }
    # Fallback to system config - add username as prefix for isolation
    sys_cfg = db.s3_system_config.find_one({'_id': 'default'})
    if sys_cfg and sys_cfg.get('endpoint_url'):
        base_prefix = sys_cfg.get('prefix', '').strip('/')
        # Each user gets their own folder: {base_prefix}/{username}/
        user_prefix = f"{base_prefix}/{username}" if base_prefix else username
        return {
            'endpoint_url': sys_cfg['endpoint_url'],
            'access_key': sys_cfg['access_key'],
            'secret_key': sys_cfg['secret_key'],
            'region': sys_cfg.get('region', ''),
            'bucket_name': sys_cfg['bucket_name'],
            'prefix': user_prefix,
            'source': 'system',
        }
    return None


def has_s3_config(db, username):
    """Check if user has any S3 config available"""
    return get_s3_config(db, username) is not None


def get_s3_client(config):
    """Create boto3 S3 client from config dict"""
    from botocore.config import Config as BotoConfig
    kwargs = {
        'aws_access_key_id': config['access_key'],
        'aws_secret_access_key': config['secret_key'],
        # Disable aws-chunked transfer encoding for S3-compatible services
        # that don't support it (boto3 >= 1.36 sends chunked + CRC32 trailers by default)
        'config': BotoConfig(request_checksum_calculation='when_required'),
    }
    if config.get('endpoint_url'):
        kwargs['endpoint_url'] = config['endpoint_url']
    if config.get('region'):
        kwargs['region_name'] = config['region']
    return boto3.client('s3', **kwargs)


def test_s3_connection(config):
    """Test S3 connection, return (success, message)"""
    try:
        client = get_s3_client(config)
        client.head_bucket(Bucket=config['bucket_name'])
        return True, "Connection successful"
    except ClientError as e:
        code = e.response['Error']['Code']
        if code == '404':
            return False, "Bucket not found"
        elif code == '403':
            return False, "Access denied"
        return False, str(e)
    except NoCredentialsError:
        return False, "Invalid credentials"
    except Exception as e:
        return False, str(e)


# ==========================================
# Workspace operations
# ==========================================

def _safe_workspace_path(username, rel_path):
    """Resolve and validate workspace path to prevent traversal"""
    base = os.path.join(WORKSPACE_ROOT, username, 'workspace')
    full = os.path.realpath(os.path.join(base, rel_path or ''))
    if not full.startswith(os.path.realpath(base)):
        return None
    return full


def list_workspace(username, rel_path=''):
    """List files/dirs in user workspace"""
    full = _safe_workspace_path(username, rel_path)
    if not full or not os.path.isdir(full):
        return None
    items = []
    for name in sorted(os.listdir(full)):
        fp = os.path.join(full, name)
        stat = os.stat(fp)
        items.append({
            'name': name,
            'type': 'dir' if os.path.isdir(fp) else 'file',
            'size': stat.st_size if os.path.isfile(fp) else 0,
            'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })
    return items


def mkdir_workspace(username, rel_path):
    """Create directory in workspace"""
    full = _safe_workspace_path(username, rel_path)
    if not full:
        return False, "Invalid path"
    try:
        os.makedirs(full, exist_ok=True)
        return True, "Created"
    except Exception as e:
        return False, str(e)


def delete_workspace(username, items, base_path=''):
    """Delete files/dirs from workspace"""
    import shutil
    deleted = []
    for item in items:
        full = _safe_workspace_path(username, os.path.join(base_path, item))
        if not full or not os.path.exists(full):
            continue
        if os.path.isdir(full):
            shutil.rmtree(full)
        else:
            os.remove(full)
        deleted.append(item)
    return deleted


def upload_to_workspace(username, rel_path, filename, file_stream):
    """Upload a file directly to workspace from HTTP upload"""
    target_dir = _safe_workspace_path(username, rel_path)
    if not target_dir:
        return False, "Invalid path"
    os.makedirs(target_dir, exist_ok=True)
    # Sanitize filename
    safe_name = os.path.basename(filename)
    if not safe_name:
        return False, "Invalid filename"
    full_path = os.path.join(target_dir, safe_name)
    # Verify still within workspace
    if not full_path.startswith(os.path.realpath(os.path.join(WORKSPACE_ROOT, username, 'workspace'))):
        return False, "Path traversal detected"
    try:
        file_stream.save(full_path)
        return True, safe_name
    except Exception as e:
        return False, str(e)


def stream_workspace_file(username, rel_path, chunk_size=1024*1024):
    """Stream a file from workspace. Returns (generator, content_length, content_type, filename) or None."""
    full = _safe_workspace_path(username, rel_path)
    if not full or not os.path.isfile(full):
        return None
    content_length = os.path.getsize(full)
    content_type, _ = mimetypes.guess_type(full)
    if not content_type:
        content_type = 'application/octet-stream'
    filename = os.path.basename(full)

    def generate():
        with open(full, 'rb') as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                yield chunk

    return generate(), content_length, content_type, filename


def read_workspace_text(username, rel_path, max_size=5*1024*1024):
    """Read text file from workspace, return content string or None. Max 5MB."""
    full = _safe_workspace_path(username, rel_path)
    if not full or not os.path.isfile(full):
        return None
    if os.path.getsize(full) > max_size:
        return None
    try:
        with open(full, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except:
        return None


# ==========================================
# S3 operations
# ==========================================

def list_s3(config, prefix=''):
    """List objects and common prefixes in S3"""
    client = get_s3_client(config)
    bucket = config['bucket_name']
    base_prefix = config.get('prefix', '').strip('/')
    if base_prefix:
        full_prefix = f"{base_prefix}/{prefix}" if prefix else f"{base_prefix}/"
    else:
        full_prefix = prefix if prefix else ''
    # Ensure trailing slash for directory listing
    if full_prefix and not full_prefix.endswith('/'):
        full_prefix += '/'

    resp = client.list_objects_v2(
        Bucket=bucket, Prefix=full_prefix, Delimiter='/'
    )
    items = []
    # Directories (common prefixes)
    for cp in resp.get('CommonPrefixes', []):
        name = cp['Prefix'][len(full_prefix):].rstrip('/')
        if name:
            items.append({'name': name, 'type': 'dir', 'size': 0, 'modified': ''})
    # Files
    for obj in resp.get('Contents', []):
        name = obj['Key'][len(full_prefix):]
        if name and name != '/':
            items.append({
                'name': name,
                'type': 'file',
                'size': obj['Size'],
                'modified': obj['LastModified'].isoformat() if obj.get('LastModified') else '',
            })
    return sorted(items, key=lambda x: (x['type'] != 'dir', x['name']))


def mkdir_s3(config, path):
    """Create a 'folder' in S3 by putting a zero-byte object"""
    client = get_s3_client(config)
    bucket = config['bucket_name']
    base_prefix = config.get('prefix', '').strip('/')
    key = f"{base_prefix}/{path}/" if base_prefix else f"{path}/"
    key = key.lstrip('/')
    client.put_object(Bucket=bucket, Key=key, Body=b'')
    return True


def delete_s3(config, items, base_path=''):
    """Delete files/folders from S3"""
    client = get_s3_client(config)
    bucket = config['bucket_name']
    base_prefix = config.get('prefix', '').strip('/')
    deleted = []
    for item in items:
        if base_prefix:
            key_prefix = f"{base_prefix}/{base_path}/{item}" if base_path else f"{base_prefix}/{item}"
        else:
            key_prefix = f"{base_path}/{item}" if base_path else item
        key_prefix = key_prefix.lstrip('/')
        # Delete object itself
        try:
            client.delete_object(Bucket=bucket, Key=key_prefix)
        except Exception:
            pass
        # Delete everything under prefix (for dirs)
        prefix_with_slash = key_prefix.rstrip('/') + '/'
        paginator = client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix_with_slash):
            for obj in page.get('Contents', []):
                client.delete_object(Bucket=bucket, Key=obj['Key'])
        deleted.append(item)
    return deleted


def upload_to_s3(config, rel_path, filename, file_data):
    """Upload a file directly to S3 from HTTP upload"""
    client = get_s3_client(config)
    bucket = config['bucket_name']
    base_prefix = config.get('prefix', '').strip('/')
    # Sanitize filename
    safe_name = os.path.basename(filename)
    if not safe_name:
        return False, "Invalid filename"
    # Build S3 key
    if base_prefix:
        s3_key = f"{base_prefix}/{rel_path}/{safe_name}" if rel_path else f"{base_prefix}/{safe_name}"
    else:
        s3_key = f"{rel_path}/{safe_name}" if rel_path else safe_name
    s3_key = s3_key.lstrip('/')
    try:
        client.put_object(Bucket=bucket, Key=s3_key, Body=file_data)
        return True, safe_name
    except Exception as e:
        return False, str(e)


# ==========================================
# Shared S3 space
# ==========================================

def get_shared_s3_config(db):
    """Get system S3 config with _shared/ prefix for shared space"""
    sys_cfg = db.s3_system_config.find_one({'_id': 'default'})
    if not sys_cfg or not sys_cfg.get('endpoint_url'):
        return None
    base_prefix = sys_cfg.get('prefix', '').strip('/')
    shared_prefix = f"{base_prefix}/_shared" if base_prefix else '_shared'
    return {
        'endpoint_url': sys_cfg['endpoint_url'],
        'access_key': sys_cfg['access_key'],
        'secret_key': sys_cfg['secret_key'],
        'region': sys_cfg.get('region', ''),
        'bucket_name': sys_cfg['bucket_name'],
        'prefix': shared_prefix,
        'source': 'shared',
    }


def get_chat_s3_config(db):
    """Get system S3 config with _chat/ prefix for chat files"""
    sys_cfg = db.s3_system_config.find_one({'_id': 'default'})
    if not sys_cfg or not sys_cfg.get('endpoint_url'):
        return None
    base_prefix = sys_cfg.get('prefix', '').strip('/')
    chat_prefix = f"{base_prefix}/_chat" if base_prefix else '_chat'
    return {
        'endpoint_url': sys_cfg['endpoint_url'],
        'access_key': sys_cfg['access_key'],
        'secret_key': sys_cfg['secret_key'],
        'region': sys_cfg.get('region', ''),
        'bucket_name': sys_cfg['bucket_name'],
        'prefix': chat_prefix,
        'source': 'chat',
    }


def list_s3_recursive(config_snapshot, s3_key_prefix):
    """List all objects recursively under a prefix (for folder share)"""
    client = get_s3_client(config_snapshot)
    bucket = config_snapshot['bucket_name']
    prefix = s3_key_prefix.rstrip('/') + '/' if s3_key_prefix else ''
    items = []
    paginator = client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            rel = obj['Key'][len(prefix):]
            if rel:
                items.append({
                    'name': rel,
                    'key': obj['Key'],
                    'size': obj['Size'],
                    'modified': obj['LastModified'].isoformat() if obj.get('LastModified') else '',
                })
    return items


def stream_s3_object(config_snapshot, s3_key, chunk_size=1024*1024):
    """Stream a single file from S3. Returns (generator, content_length, content_type)."""
    client = get_s3_client(config_snapshot)
    bucket = config_snapshot['bucket_name']
    resp = client.get_object(Bucket=bucket, Key=s3_key)
    content_length = resp['ContentLength']
    content_type = resp.get('ContentType', 'application/octet-stream')
    # Guess better content type from key name
    guessed, _ = mimetypes.guess_type(s3_key)
    if guessed:
        content_type = guessed

    def generate():
        body = resp['Body']
        while True:
            chunk = body.read(chunk_size)
            if not chunk:
                break
            yield chunk
        body.close()

    return generate(), content_length, content_type


def read_s3_text(config_snapshot, s3_key, max_size=5*1024*1024):
    """Read text file from S3, return content string or None. Max 5MB."""
    try:
        client = get_s3_client(config_snapshot)
        bucket = config_snapshot['bucket_name']
        head = client.head_object(Bucket=bucket, Key=s3_key)
        if head['ContentLength'] > max_size:
            return None
        resp = client.get_object(Bucket=bucket, Key=s3_key)
        return resp['Body'].read().decode('utf-8', errors='replace')
    except:
        return None


MAX_ZIP_SIZE = 2 * 1024 * 1024 * 1024  # 2GB limit


def stream_s3_folder_as_zip(config_snapshot, s3_key_prefix):
    """Build a zip in memory from all files under a prefix, return bytes generator.
    Limit total uncompressed size to 2GB."""
    client = get_s3_client(config_snapshot)
    bucket = config_snapshot['bucket_name']
    prefix = s3_key_prefix.rstrip('/') + '/' if s3_key_prefix else ''

    buf = io.BytesIO()
    total_size = 0
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        paginator = client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get('Contents', []):
                rel = obj['Key'][len(prefix):]
                if not rel:
                    continue
                total_size += obj['Size']
                if total_size > MAX_ZIP_SIZE:
                    raise ValueError("Folder too large (>2GB), cannot zip")
                data = client.get_object(Bucket=bucket, Key=obj['Key'])['Body'].read()
                zf.writestr(rel, data)

    buf.seek(0)
    zip_bytes = buf.getvalue()

    def generate():
        chunk_size = 1024 * 1024
        offset = 0
        while offset < len(zip_bytes):
            yield zip_bytes[offset:offset + chunk_size]
            offset += chunk_size

    return generate(), len(zip_bytes)


# ==========================================
# Transfer engine
# ==========================================

MULTIPART_THRESHOLD = 100 * 1024 * 1024  # 100MB


def _do_transfer(task_id, username, config, source, dest, items, source_path, dest_path):
    """Background transfer worker"""
    task = _tasks[task_id]
    total = len(items)
    task['total'] = total
    client = get_s3_client(config)
    bucket = config['bucket_name']
    base_prefix = config.get('prefix', '').strip('/')

    errors = []
    for i, item_name in enumerate(items):
        task['current_file'] = item_name
        task['completed'] = i
        try:
            if source == 'workspace' and dest == 's3':
                _upload_item(client, bucket, base_prefix, username, source_path, dest_path, item_name, task)
            elif source == 's3' and dest == 'workspace':
                _download_item(client, bucket, base_prefix, username, source_path, dest_path, item_name, task)
        except Exception as e:
            errors.append(f"{item_name}: {e}")

    task['completed'] = total
    if errors:
        task['status'] = 'error'
        task['error'] = f"{len(errors)} error(s): {errors[0]}"
    else:
        task['status'] = 'done'


def _upload_item(client, bucket, base_prefix, username, src_path, dst_path, item_name, task):
    """Upload file or directory from workspace to S3"""
    local_base = _safe_workspace_path(username, os.path.join(src_path, item_name))
    if not local_base:
        return
    if base_prefix:
        s3_base = f"{base_prefix}/{dst_path}/{item_name}" if dst_path else f"{base_prefix}/{item_name}"
    else:
        s3_base = f"{dst_path}/{item_name}" if dst_path else item_name
    s3_base = s3_base.lstrip('/')

    if os.path.isfile(local_base):
        _upload_file(client, bucket, local_base, s3_base, task)
    elif os.path.isdir(local_base):
        errors = []
        for root, dirs, files in os.walk(local_base):
            for f in files:
                local_fp = os.path.join(root, f)
                rel = os.path.relpath(local_fp, local_base)
                s3_key = f"{s3_base}/{rel}".replace('\\', '/')
                try:
                    _upload_file(client, bucket, local_fp, s3_key, task)
                except Exception as e:
                    errors.append(f"{rel}: {e}")
        if errors:
            raise Exception(f"{len(errors)} file(s) failed: {errors[0]}")


def _upload_file(client, bucket, local_path, s3_key, task):
    """Upload single file to S3"""
    size = os.path.getsize(local_path)
    task['current_file'] = os.path.basename(local_path)
    if size > MULTIPART_THRESHOLD:
        # Multipart upload for large files
        from boto3.s3.transfer import TransferConfig
        config = TransferConfig(
            multipart_threshold=MULTIPART_THRESHOLD,
            max_concurrency=4,
            multipart_chunksize=8 * 1024 * 1024,
        )
        client.upload_file(local_path, bucket, s3_key, Config=config)
    else:
        # Read into bytes so Content-Length is always deterministic
        with open(local_path, 'rb') as f:
            data = f.read()
        client.put_object(Bucket=bucket, Key=s3_key, Body=data)


def _download_item(client, bucket, base_prefix, username, src_path, dst_path, item_name, task):
    """Download file or directory from S3 to workspace"""
    if base_prefix:
        s3_key = f"{base_prefix}/{src_path}/{item_name}" if src_path else f"{base_prefix}/{item_name}"
    else:
        s3_key = f"{src_path}/{item_name}" if src_path else item_name
    s3_key = s3_key.lstrip('/')

    local_base = _safe_workspace_path(username, os.path.join(dst_path, item_name))
    if not local_base:
        return

    # Check if it's a "directory" in S3
    prefix = s3_key.rstrip('/') + '/'
    resp = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    is_dir = resp.get('KeyCount', 0) > 0

    if is_dir:
        paginator = client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get('Contents', []):
                rel = obj['Key'][len(prefix):]
                if not rel:
                    continue
                local_fp = os.path.join(local_base, rel.replace('/', os.sep))
                os.makedirs(os.path.dirname(local_fp), exist_ok=True)
                client.download_file(bucket, obj['Key'], local_fp)
    else:
        # Single file
        os.makedirs(os.path.dirname(local_base), exist_ok=True)
        try:
            client.download_file(bucket, s3_key, local_base)
        except ClientError:
            # Try with trailing slash removed (might be empty dir marker)
            pass


def start_transfer(username, config, source, dest, items, source_path='', dest_path=''):
    """Start a background transfer, return task_id"""
    task_id = str(uuid.uuid4())[:8]
    task = {
        'id': task_id,
        'status': 'running',
        'total': len(items),
        'completed': 0,
        'current_file': '',
        'error': None,
        'username': username,
    }
    with _tasks_lock:
        _tasks[task_id] = task

    t = threading.Thread(
        target=_do_transfer,
        args=(task_id, username, config, source, dest, items, source_path, dest_path),
        daemon=True
    )
    t.start()
    return task_id


def get_transfer_status(task_id):
    """Get status of a transfer task"""
    with _tasks_lock:
        task = _tasks.get(task_id)
    if not task:
        return None
    return {
        'id': task['id'],
        'status': task['status'],
        'total': task['total'],
        'completed': task['completed'],
        'current_file': task['current_file'],
        'error': task['error'],
    }


# ==========================================
# S3 Move/Copy operations
# ==========================================

def move_s3_items(config, items, source_path, dest_path, operation='move'):
    """Move or copy items within S3.

    Args:
        config: S3 config dict
        items: list of item names (files or folders)
        source_path: relative source directory path
        dest_path: relative destination directory path
        operation: 'move' or 'copy'

    Returns:
        (success_count, error_list)
    """
    client = get_s3_client(config)
    bucket = config['bucket_name']
    base_prefix = config.get('prefix', '').strip('/')

    success_count = 0
    errors = []

    for item_name in items:
        try:
            # Build source key
            if base_prefix:
                src_key = f"{base_prefix}/{source_path}/{item_name}" if source_path else f"{base_prefix}/{item_name}"
            else:
                src_key = f"{source_path}/{item_name}" if source_path else item_name
            src_key = src_key.lstrip('/')

            # Build dest key
            if base_prefix:
                dst_key = f"{base_prefix}/{dest_path}/{item_name}" if dest_path else f"{base_prefix}/{item_name}"
            else:
                dst_key = f"{dest_path}/{item_name}" if dest_path else item_name
            dst_key = dst_key.lstrip('/')

            # Skip if source == dest
            if src_key == dst_key:
                continue

            # Check if it's a "directory" (has objects under prefix/)
            prefix_check = src_key.rstrip('/') + '/'
            resp = client.list_objects_v2(Bucket=bucket, Prefix=prefix_check, MaxKeys=1)
            is_dir = resp.get('KeyCount', 0) > 0

            if is_dir:
                # Copy all objects under the prefix
                paginator = client.get_paginator('list_objects_v2')
                for page in paginator.paginate(Bucket=bucket, Prefix=prefix_check):
                    for obj in page.get('Contents', []):
                        rel = obj['Key'][len(prefix_check):]
                        new_key = dst_key.rstrip('/') + '/' + rel
                        # Copy
                        client.copy_object(
                            Bucket=bucket,
                            CopySource={'Bucket': bucket, 'Key': obj['Key']},
                            Key=new_key
                        )
                        # Delete original if move
                        if operation == 'move':
                            client.delete_object(Bucket=bucket, Key=obj['Key'])
            else:
                # Single file
                try:
                    client.copy_object(
                        Bucket=bucket,
                        CopySource={'Bucket': bucket, 'Key': src_key},
                        Key=dst_key
                    )
                    if operation == 'move':
                        client.delete_object(Bucket=bucket, Key=src_key)
                except ClientError:
                    # Maybe it's a folder marker
                    pass

            success_count += 1
        except Exception as e:
            errors.append(f"{item_name}: {e}")

    return success_count, errors


def copy_s3_to_workspace(config_snapshot, s3_key, item_type, username, dest_path='', item_name=None):
    """Copy file/folder from sender's S3 to recipient's workspace.

    Args:
        config_snapshot: S3 config dict (from share)
        s3_key: full S3 key of the item
        item_type: 'file' or 'dir'
        username: recipient username
        dest_path: relative destination path in workspace
        item_name: name to save as (optional, defaults to basename of s3_key)

    Returns:
        (success, message)
    """
    import shutil

    client = get_s3_client(config_snapshot)
    bucket = config_snapshot['bucket_name']

    if not item_name:
        item_name = s3_key.rsplit('/', 1)[-1] if '/' in s3_key else s3_key

    local_base = _safe_workspace_path(username, os.path.join(dest_path, item_name) if dest_path else item_name)
    if not local_base:
        return False, "Invalid destination path"

    try:
        if item_type == 'file':
            # Single file download
            os.makedirs(os.path.dirname(local_base), exist_ok=True)
            client.download_file(bucket, s3_key, local_base)
        else:
            # Directory download
            prefix = s3_key.rstrip('/') + '/'
            paginator = client.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get('Contents', []):
                    rel = obj['Key'][len(prefix):]
                    if not rel:
                        continue
                    local_fp = os.path.join(local_base, rel.replace('/', os.sep))
                    os.makedirs(os.path.dirname(local_fp), exist_ok=True)
                    client.download_file(bucket, obj['Key'], local_fp)

        return True, f"Copied to workspace: {item_name}"
    except Exception as e:
        return False, str(e)
