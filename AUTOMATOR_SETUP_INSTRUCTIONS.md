# macOS Automator Directory Action Setup

## Quick Setup Guide

Follow these steps to set up automatic transcription for **NEW** Google Meet recordings only.

**Important**: Existing MP4 files in the folder will be **ignored**. Only new files added after setup will be processed.

### Step 1: Open Automator
1. Open **Automator** (⌘ + Space, type "Automator")
2. Click **New Document**
3. Select **Folder Action** as the document type
4. Click **Choose**

### Step 2: Configure Folder Monitoring
1. At the top of the workflow, you'll see "Folder Action receives files and folders added to"
2. Click the dropdown menu and select **Other...**
3. Navigate to:
   ```
   /Users/omri.a/Library/CloudStorage/GoogleDrive-omri.a@taboola.com/My Drive/Meet Recordings
   ```
4. Click **Choose**

### Step 3: Add Run Shell Script Action
1. In the left sidebar, search for "Run Shell Script"
2. Drag **Run Shell Script** to the workflow area on the right
3. Configure the action:
   - **Shell**: `/bin/bash` (default)
   - **Pass input**: `as arguments`

4. Replace the default script content with:
   ```bash
   for f in "$@"
   do
       /Users/omri.a/Code/speech-to-text-tools/auto_transcribe_meet.sh "$f"
   done
   ```

### Step 4: Save the Workflow
1. Press **⌘ + S** to save
2. Name it: `Auto Transcribe Meet Recordings`
3. Location: It will automatically save to `~/Library/Workflows/Applications/Folder Actions/`
4. Click **Save**

### Step 5: Enable Folder Actions (if needed)
1. Right-click on the Meet Recordings folder in Finder
2. Go to **Services** → **Folder Actions Setup**
3. Make sure "Enable Folder Actions" is checked
4. Your workflow should appear in the list

## How It Works

✅ **NEW files only**: Folder Action triggers ONLY when you add a new MP4 file
❌ **Existing files ignored**: Files already in the folder before setup are not processed

When you add a new MP4 file to the Meet Recordings folder:

1. **Folder Action triggers** automatically
2. **Script executes** in the background
3. **Three files are created** in the same folder:
   - `YYYY-MM-DD-HH-MM-Meeting-Name.m4a` (audio)
   - `YYYY-MM-DD-HH-MM-Meeting-Name-en.txt` (English transcript)
   - `YYYY-MM-DD-HH-MM-Meeting-Name-he.txt` (Hebrew transcript)

## Testing

To test the setup, copy a file INTO the monitored folder:

```bash
# Copy a test file to trigger the folder action
cp /Users/omri.a/Code/speech-to-text-tools/test.mp4 \
   "/Users/omri.a/Library/CloudStorage/GoogleDrive-omri.a@taboola.com/My Drive/Meet Recordings/test-recording.mp4"
```

Watch for the new files to appear in the same folder (takes 5-7 minutes).

## Monitoring

Check the log file to see progress:
```bash
tail -f /tmp/auto_transcribe.log
```

## Files Created

All files are saved in the **same folder** as the original MP4:

```
Meet Recordings/
├── Team Standup.mp4                          # Original (unchanged)
├── 2025-11-23-14-30-Team-Standup.m4a        # Audio extracted
├── 2025-11-23-14-30-Team-Standup-en.txt     # English transcript
└── 2025-11-23-14-30-Team-Standup-he.txt     # Hebrew transcript
```

Timestamp is extracted from the **file creation date**.

## Performance Notes

For a typical 30-minute meeting:
- **Audio extraction**: ~10-30 seconds
- **English transcription**: ~2-3 minutes
- **Hebrew transcription**: ~2-3 minutes
- **Total time**: ~5-7 minutes

The script runs in the background - you don't need to wait.

## Troubleshooting

### Folder Action Not Triggering
1. Right-click the folder → Services → Folder Actions Setup
2. Verify "Enable Folder Actions" is checked
3. Verify your workflow is listed and enabled for the folder

### Test Manually
Run the script directly on any MP4:
```bash
/Users/omri.a/Code/speech-to-text-tools/auto_transcribe_meet.sh \
  "/Users/omri.a/Library/CloudStorage/GoogleDrive-omri.a@taboola.com/My Drive/Meet Recordings/test.mp4"
```

### Check Logs
View processing logs:
```bash
cat /tmp/auto_transcribe.log
```

### Permissions Issue
Ensure script is executable:
```bash
chmod +x /Users/omri.a/Code/speech-to-text-tools/auto_transcribe_meet.sh
```

## Advanced Configuration

### Change Whisper Model
Edit `/Users/omri.a/Code/speech-to-text-tools/auto_transcribe_meet.sh` line 15:

```bash
WHISPER_MODEL="medium"  # Options: tiny, base, small, medium, large
```

- `tiny/base`: Faster but less accurate
- `medium`: Good balance (default) ⭐
- `large`: Best accuracy but 2x slower

### Change Audio Bitrate
Edit the script, line 85:

```bash
--bitrate 192k  # Options: 128k, 192k, 256k, 320k
```

## What About Existing Files?

If you want to process existing MP4 files manually:

```bash
cd "/Users/omri.a/Library/CloudStorage/GoogleDrive-omri.a@taboola.com/My Drive/Meet Recordings"

# Process a single file
/Users/omri.a/Code/speech-to-text-tools/auto_transcribe_meet.sh "existing-meeting.mp4"

# Or process all MP4 files
for f in *.mp4; do
    /Users/omri.a/Code/speech-to-text-tools/auto_transcribe_meet.sh "$f"
done
```

But **Folder Actions will NOT automatically process existing files**.
