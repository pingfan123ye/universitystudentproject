import { useState, useRef, useCallback } from 'react';

/**
 * 前端录音 Hook — MediaRecorder 录制 → base64
 * 搭配后端 faster-whisper 离线转写
 */
export function useAudioRecorder() {
  const [recording, setRecording] = useState(false);
  const [processing, setProcessing] = useState(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);

  const isSupported =
    typeof window !== 'undefined' && 'MediaRecorder' in window;

  /** 开始录音 */
  const startRecording = useCallback(async (): Promise<boolean> => {
    if (!isSupported) return false;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;

      const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus'
        : 'audio/webm';

      chunksRef.current = [];
      const mr = new MediaRecorder(stream, { mimeType });
      mr.ondataavailable = (e: BlobEvent) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      mediaRecorderRef.current = mr;
      mr.start(500);
      setRecording(true);
      return true;
    } catch {
      return false;
    }
  }, [isSupported]);

  /** 停止录音并返回 base64 音频数据 */
  const stopRecording = useCallback(async (): Promise<string> => {
    const mr = mediaRecorderRef.current;
    if (!mr || mr.state === 'inactive') return '';

    return new Promise<string>((resolve) => {
      mr.onstop = () => {
        const blob = new Blob(chunksRef.current, { type: mr.mimeType || 'audio/webm' });
        chunksRef.current = [];
        if (streamRef.current) {
          streamRef.current.getTracks().forEach((t) => t.stop());
          streamRef.current = null;
        }
        const reader = new FileReader();
        reader.onloadend = () => {
          const result = reader.result as string;
          const base64 = result.includes(',') ? result.split(',')[1] : result;
          setRecording(false);
          resolve(base64);
        };
        reader.onerror = () => { setRecording(false); resolve(''); };
        reader.readAsDataURL(blob);
      };
      mr.stop();
    });
  }, []);

  return { isSupported, recording, processing, setProcessing, startRecording, stopRecording };
}
