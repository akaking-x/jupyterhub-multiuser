#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
S3 Backup Manager
Handles S3 operations, workspace browsing, and file transfers
"""

import os
import uuid
import threading
from datetime import datetime

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

# In-memory transfer task tracking
_tasks = {}
_tasks_lock = threading.Lock()

WORKSPACE_ROOT = '/home'


def get_s3_config(db, username):
    """Get S3 config for user: personal first, then system fallback"""
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
    # Fallback to system config
    sys_cfg = db.s3_system_config.find_one({'_id': 'default'})
    if sys_cfg and sys_cfg.get('endpoint_url'):
        return {
            'endpoint_url': sys_cfg['endpoint_url'],
            'access_key': sys_cfg['access_key'],
            'secret_key': sys_cfg['secret_key'],
            'region': sys_cfg.get('region', ''),
            'bucket_name': sys_cfg['bucket_name'],
            'prefix': sys_cfg.get('prefix', ''),
            'source': 'system',
        }
    return None


def has_s3_config(db, username):
    """Check if user has any S3 config available"""
    return get_s3_config(db, username) is not None


def get_s3_client(config):
    """Create boto3 S3 client from config dict"""
    kwargs = {
        'aws_access_key_id': config['access_key'],
        'aws_secret_access_key': config['secret_key'],
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
