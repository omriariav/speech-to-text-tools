# macOS Automator Folder Action Setup

Automatically transcribe video/audio files when added to a folder. Includes speaker diarization (identifies different speakers) and generates both English and Hebrew transcripts.

## Quick Setup Guide

Follow these steps to set up automatic transcription for **NEW** files only.

**Important**: Existing files in the folder will be **ignored**. Only new files added after setup will be processed.

---

## Option A: Google Meet Recordings Folder

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
3. **Up to five files are created** in the output directory:
   - `YYYY-MM-DD-HH-MM-Meeting-Name.m4a` (audio)
   - `YYYY-MM-DD-HH-MM-Meeting-Name-he.txt` (Hebrew transcript, fast)
   - `YYYY-MM-DD-HH-MM-Meeting-Name-en.txt` (English transcript, fast)
   - `YYYY-MM-DD-HH-MM-Meeting-Name-he-diarized.txt` (Hebrew with speakers)
   - `YYYY-MM-DD-HH-MM-Meeting-Name-en-diarized.txt` (English with speakers)

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

All output files are saved to the configured `OUTPUT_DIR`:

```
meetings_context/
├── 2025-11-23-14-30-Team-Standup.m4a              # Audio extracted
├── 2025-11-23-14-30-Team-Standup-he.txt           # Hebrew (fast, no speakers)
├── 2025-11-23-14-30-Team-Standup-en.txt           # English (fast, no speakers)
├── 2025-11-23-14-30-Team-Standup-he-diarized.txt  # Hebrew (with speakers)
└── 2025-11-23-14-30-Team-Standup-en-diarized.txt  # English (with speakers)
```

Fast transcripts are available within minutes. Diarized transcripts follow afterward.

## Performance Notes

For a typical 30-minute meeting:
- **Audio extraction**: ~10-30 seconds
- **Fast transcription** (HE + EN, large model): ~5-10 minutes — transcripts available immediately
- **Diarized transcription** (HE + EN, medium model + Pyannote): ~10-16 minutes — runs after fast

Fast-only (`ENABLE_DIARIZATION=false`): ~5-10 minutes total
Diarize-only (`ENABLE_FAST=false`): ~10-17 minutes total

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

Edit `/Users/omri.a/Code/speech-to-text-tools/auto_transcribe_meet.sh`:

### Toggle Transcription Modes
```bash
ENABLE_FAST=true           # Fast Whisper-only transcription (default: true)
ENABLE_DIARIZATION=true    # Diarized with speaker IDs (default: true)
```

### Change Whisper Models
```bash
FAST_MODEL="large"         # Model for fast transcription (default: large)
DIARIZE_MODEL="medium"     # Model for diarized transcription (default: medium)
```

Model options: `tiny`, `base`, `small`, `medium`, `large`

### Change Audio Bitrate
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

---

## Option B: Downloads Folder

Monitor your Downloads folder and automatically transcribe any MP4/M4A files.

### Step 1: Open Automator
1. Open **Automator** (⌘ + Space, type "Automator")
2. Click **New Document**
3. Select **Folder Action** as the document type
4. Click **Choose**

### Step 2: Configure Folder Monitoring
1. At the top of the workflow, you'll see "Folder Action receives files and folders added to"
2. Click the dropdown menu and select **Other...**
3. Navigate to: `/Users/omri.a/Downloads`
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
       if [[ "$f" =~ \.(mp4|MP4|m4a|M4A)$ ]]; then
           /Users/omri.a/Code/speech-to-text-tools/auto_transcribe_meet.sh "$f"
       fi
   done
   ```

   > **Note**: This script includes a file type filter so only MP4/M4A files trigger transcription. Other downloads (PDFs, images, etc.) are ignored.

### Step 4: Save the Workflow
1. Press **⌘ + S** to save
2. Name it: `Auto Transcribe Downloads`
3. Click **Save**

### Step 5: Enable Folder Actions (if needed)
1. Right-click on the Downloads folder in Finder
2. Go to **Services** → **Folder Actions Setup**
3. Make sure "Enable Folder Actions" is checked
4. Your workflow should appear in the list

---

## Speaker Diarization

The script automatically identifies different speakers in your recordings:

**English transcripts:**
```
Speaker 1: Hello, welcome to the meeting.
Speaker 2: Thanks for having me.
```

**Hebrew transcripts:**
```
דובר 1: שלום, ברוכים הבאים לפגישה.
דובר 2: תודה על ההזמנה.
```

To disable speaker diarization, edit `auto_transcribe_meet.sh` and set:
```bash
ENABLE_DIARIZATION=false  # Only fast transcripts will be produced
```
