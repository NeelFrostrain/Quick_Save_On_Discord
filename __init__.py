bl_info = {
    "name": "Quick Save On Discord (.blend → .7z)",
    "author": "Neel Frostrain",
    "version": (0, 1, 33),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Discord",
    "description": "Compress .blend to .7z and upload to Discord only on real changes (hash + depsgraph + cooldown)",
    "category": "System",
}

import bpy
import os
import threading
import tempfile
import urllib.request
import subprocess
import hashlib
import json
import time

# ------------------------------------------------------
# GLOBAL STATE
# ------------------------------------------------------

_REAL_CHANGE_DETECTED = False
_CHANGE_TYPES = set()

# ------------------------------------------------------
# PATHS
# ------------------------------------------------------

ADDON_DIR = os.path.dirname(os.path.realpath(__file__))
SEVEN_ZIP_PATH = os.path.join(ADDON_DIR, r"modules\7-Zip\7z.exe")

PARTIAL_HASH_CHUNK = 2 * 1024 * 1024  # 2 MB

# ------------------------------------------------------
# PARTIAL HASH (FAST + SAFE)
# ------------------------------------------------------

def compute_partial_hash(path):
    size = os.path.getsize(path)
    h = hashlib.sha1()

    with open(path, "rb") as f:
        h.update(f.read(PARTIAL_HASH_CHUNK))
        if size > PARTIAL_HASH_CHUNK:
            f.seek(max(size - PARTIAL_HASH_CHUNK, 0))
            h.update(f.read(PARTIAL_HASH_CHUNK))

    h.update(str(size).encode("utf-8"))
    return h.hexdigest()

# ------------------------------------------------------
# UI HELPERS (THREAD SAFE)
# ------------------------------------------------------

def set_status(text=None):
    bpy.app.timers.register(lambda: bpy.context.workspace.status_text_set(text))

def clear_status(delay=2.0):
    bpy.app.timers.register(
        lambda: bpy.context.workspace.status_text_set(None),
        first_interval=delay
    )

def report_info(msg):
    bpy.app.timers.register(
        lambda: bpy.ops.wm.report(type={'INFO'}, message=msg)
    )

def show_no_change_status():
    report_info("No changes detected — nothing to upload")
    set_status("⏭️ No changes detected — nothing to upload")
    clear_status(2.0)

def show_cooldown_status(remaining):
    msg = f"⏳ Cooldown active — wait {int(remaining)} sec"
    report_info(msg)
    set_status(msg)
    clear_status(2.0)

# ------------------------------------------------------
# PROJECT SETTINGS
# ------------------------------------------------------

class DiscordProjectSettings(bpy.types.PropertyGroup):
    webhook_url: bpy.props.StringProperty(
        name="Webhook URL",
        default="",
        maxlen=1024
    )

    auto_send: bpy.props.BoolProperty(
        name="Auto Send on Save",
        default=False
    )

    commit_message: bpy.props.StringProperty(
        name="Commit Message",
        description="Optional commit-style message",
        default=""
    )

    cooldown_seconds: bpy.props.IntProperty(
        name="Cooldown (seconds)",
        default=30,
        min=0,
        max=3600
    )

    last_send_time: bpy.props.FloatProperty(
        default=0.0,
        options={'HIDDEN'}
    )

    last_file_hash: bpy.props.StringProperty(
        default="",
        options={'HIDDEN'}
    )

# ------------------------------------------------------
# COOLDOWN (FIXED — NO BUGS)
# ------------------------------------------------------

def is_cooldown_active(settings):
    # Never sent before → no cooldown
    if settings.last_send_time <= 0.0:
        return False, 0

    if settings.cooldown_seconds <= 0:
        return False, 0

    elapsed = time.monotonic() - settings.last_send_time
    remaining = settings.cooldown_seconds - elapsed

    return remaining > 0, max(0, remaining)

# ------------------------------------------------------
# SAVE TYPE
# ------------------------------------------------------

def is_autosave(filepath):
    name = os.path.basename(filepath).lower()
    return "autosave" in name or "quit" in name

# ------------------------------------------------------
# DEPSGRAPH (REAL USER EDITS)
# ------------------------------------------------------

def on_depsgraph_update(scene, depsgraph):
    global _REAL_CHANGE_DETECTED, _CHANGE_TYPES

    if _REAL_CHANGE_DETECTED:
        return

    for u in depsgraph.updates:
        if u.is_updated_transform:
            _CHANGE_TYPES.add("Transform")
        if u.is_updated_geometry:
            _CHANGE_TYPES.add("Geometry")
        if u.is_updated_shading:
            _CHANGE_TYPES.add("Shading")

        if _CHANGE_TYPES:
            _REAL_CHANGE_DETECTED = True
            return

# ------------------------------------------------------
# CORE
# ------------------------------------------------------

def compress_blend_7z(filepath):
    out = os.path.join(
        tempfile.gettempdir(),
        os.path.basename(filepath).replace(".blend", ".7z")
    )

    subprocess.run(
        [SEVEN_ZIP_PATH, "a", "-t7z", "-mx=9", "-m0=lzma2", out, filepath],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True
    )

    return out

def build_commit_message(settings):
    if settings.commit_message.strip():
        return settings.commit_message.strip()
    if _CHANGE_TYPES:
        return "Update: " + ", ".join(sorted(_CHANGE_TYPES))
    return "Update: Scene modified"

def send_to_discord(webhook, archive, message):
    with open(archive, "rb") as f:
        file_data = f.read()

    boundary = "----BlenderDiscordBoundary"
    payload = json.dumps({"content": message}).encode("utf-8")

    body = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"payload_json\"\r\n"
        f"Content-Type: application/json\r\n\r\n"
    ).encode() + payload + (
        f"\r\n--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"; filename=\"{os.path.basename(archive)}\"\r\n"
        f"Content-Type: application/x-7z-compressed\r\n\r\n"
    ).encode() + file_data + (
        f"\r\n--{boundary}--\r\n"
    ).encode()

    req = urllib.request.Request(
        webhook,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
            "User-Agent": "BlenderDiscordUploader",
        },
        method="POST"
    )

    urllib.request.urlopen(req, timeout=30)

# ------------------------------------------------------
# BACKGROUND SEND
# ------------------------------------------------------

def process_send(filepath, settings, autosave, current_hash):
    global _REAL_CHANGE_DETECTED, _CHANGE_TYPES

    try:
        set_status("📦 Compressing project...")
        archive = compress_blend_7z(filepath)

        set_status("📡 Uploading to Discord...")
        send_to_discord(
            settings.webhook_url,
            archive,
            build_commit_message(settings)
        )

        def save_state():
            settings.last_file_hash = current_hash
            settings.last_send_time = time.monotonic()
            settings.commit_message = ""
            _REAL_CHANGE_DETECTED = False
            _CHANGE_TYPES.clear()
            return None

        bpy.app.timers.register(save_state)

        if not autosave:
            set_status("✅ Upload complete")
            clear_status(3.0)

    except Exception as e:
        report_info(str(e))
        clear_status(3.0)

# ------------------------------------------------------
# SAVE HANDLER (ABSOLUTE GATE)
# ------------------------------------------------------

def on_save_post(dummy):
    settings = bpy.context.scene.discord_project_settings
    filepath = bpy.data.filepath

    if not settings.auto_send or not filepath or not settings.webhook_url:
        return

    autosave = is_autosave(filepath)

    cooldown, remaining = is_cooldown_active(settings)
    if cooldown:
        if not autosave:
            show_cooldown_status(remaining)
        return

    current_hash = compute_partial_hash(filepath)

    if current_hash == settings.last_file_hash or not _REAL_CHANGE_DETECTED:
        if not autosave:
            show_no_change_status()
        return

    threading.Thread(
        target=process_send,
        args=(filepath, settings, autosave, current_hash),
        daemon=True
    ).start()

# ------------------------------------------------------
# SEND NOW
# ------------------------------------------------------

class DISCORDSEND_OT_SendNow(bpy.types.Operator):
    bl_idname = "discord.send_now"
    bl_label = "Send Now"

    def execute(self, context):
        settings = context.scene.discord_project_settings
        filepath = bpy.data.filepath

        cooldown, remaining = is_cooldown_active(settings)
        if cooldown:
            show_cooldown_status(remaining)
            return {'FINISHED'}

        current_hash = compute_partial_hash(filepath)

        if current_hash == settings.last_file_hash or not _REAL_CHANGE_DETECTED:
            show_no_change_status()
            return {'FINISHED'}

        threading.Thread(
            target=process_send,
            args=(filepath, settings, False, current_hash),
            daemon=True
        ).start()

        return {'FINISHED'}

# ------------------------------------------------------
# UI PANEL
# ------------------------------------------------------

class DISCORDSEND_PT_Panel(bpy.types.Panel):
    bl_label = "Quick Save On Discord"
    bl_idname = "DISCORDSEND_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Discord'

    def draw(self, context):
        s = context.scene.discord_project_settings
        layout = self.layout
        layout.prop(s, "webhook_url")
        layout.prop(s, "auto_send")
        layout.prop(s, "commit_message")
        layout.prop(s, "cooldown_seconds")
        layout.operator("discord.send_now", icon="EXPORT")

# ------------------------------------------------------
# REGISTER
# ------------------------------------------------------

classes = (
    DiscordProjectSettings,
    DISCORDSEND_OT_SendNow,
    DISCORDSEND_PT_Panel,
)

def register():
    for c in classes:
        bpy.utils.register_class(c)

    bpy.types.Scene.discord_project_settings = bpy.props.PointerProperty(
        type=DiscordProjectSettings
    )

    bpy.app.handlers.depsgraph_update_post.append(on_depsgraph_update)
    bpy.app.handlers.save_post.append(on_save_post)

def unregister():
    bpy.app.handlers.depsgraph_update_post.remove(on_depsgraph_update)
    bpy.app.handlers.save_post.remove(on_save_post)
    del bpy.types.Scene.discord_project_settings

    for c in reversed(classes):
        bpy.utils.unregister_class(c)

if __name__ == "__main__":
    register()
