/**
 * Mirrored webcam self-view for the talk screen.
 * Video only: conversation audio still runs in Python.
 */

import { h } from "../ui.js";

export function createWebcamPreview() {
  const video = h("video", { class: "talk__cam-video" });
  video.autoplay = true;
  video.muted = true;
  video.playsInline = true;

  const root = h(
    "div",
    { class: "talk__cam", "aria-hidden": "true" },
    video,
    h("span", { class: "talk__cam-label" }, "camera")
  );

  let stream = null;
  let starting = false;
  let stopped = false;
  let generation = 0;
  let permissionStatus = null;

  void watchPermission();

  async function start() {
    if (stopped || stream || starting) return;
    if (!navigator.mediaDevices?.getUserMedia) {
      console.warn("Webcam preview unavailable: getUserMedia is not supported");
      return;
    }
    starting = true;
    const ticket = ++generation;
    let next = null;
    try {
      next = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "user" },
        audio: false,
      });
    } catch (error) {
      console.warn("Webcam preview declined or failed", error);
      return;
    } finally {
      starting = false;
    }
    if (!current(ticket)) {
      stopTracks(next);
      return;
    }
    stream = next;
    video.srcObject = stream;
    try {
      await video.play();
    } catch {
      // A muted video usually autoplays; the stream stays attached either way.
    }
    if (!current(ticket)) return;
    root.classList.add("talk__cam--visible");
    root.setAttribute("aria-hidden", "false");
    root.setAttribute("aria-label", "Webcam preview");
  }

  function current(ticket) {
    return !stopped && ticket === generation;
  }

  function hide() {
    generation += 1;
    releaseStream();
    root.classList.remove("talk__cam--visible");
    root.setAttribute("aria-hidden", "true");
    root.removeAttribute("aria-label");
  }

  function stop() {
    stopped = true;
    permissionStatus?.removeEventListener("change", onPermissionChange);
    permissionStatus = null;
    hide();
  }

  function releaseStream() {
    stopTracks(stream);
    stream = null;
    video.srcObject = null;
  }

  function stopTracks(mediaStream) {
    if (!mediaStream) return;
    for (const track of mediaStream.getTracks()) track.stop();
  }

  function onPermissionChange() {
    if (stopped || !permissionStatus) return;
    if (permissionStatus.state === "granted") void start();
    else if (permissionStatus.state === "denied") hide();
  }

  async function watchPermission() {
    try {
      const status = await navigator.permissions?.query?.({ name: "camera" });
      if (!status || stopped) return;
      permissionStatus = status;
      status.addEventListener("change", onPermissionChange);
    } catch {
      // The Permissions API does not support "camera" in every browser.
    }
  }

  return { root, start, stop };
}
