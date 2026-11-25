"""Decaid app and machine firmware updates, reported but not installed.

Installing either from Home Assistant is deliberately unsupported: an app
update needs approval on the tablet, and a firmware flash should be watched
at the machine.
"""

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.const import EntityCategory

from .entity import DecaidEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DecaidAppUpdate(entry), DecaidFirmwareUpdate(entry)])


def _text(value):
    return value if isinstance(value, str) and value else None


class DecaidAppUpdate(DecaidEntity, UpdateEntity):
    """The Decaid app on the tablet."""

    _attr_name = "Decaid app"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # Progress of an install started on the tablet; INSTALL is never offered.
    _attr_supported_features = UpdateEntityFeature.PROGRESS

    def __init__(self, entry):
        super().__init__(entry.runtime_data.app_update, entry, "app_update")

    @property
    def available(self):
        return super().available and self.installed_version is not None

    @property
    def installed_version(self):
        return _text(self.payload.get("currentVersion"))

    @property
    def latest_version(self):
        # Decaid sets latestVersion only while it knows of a newer release, so
        # it stays set through a recheck or a failed download.
        return _text(self.payload.get("latestVersion")) or self.installed_version

    @property
    def in_progress(self):
        return self.payload.get("phase") in ("downloading", "installing")

    @property
    def update_percentage(self):
        progress = self.payload.get("progress")
        if not self.in_progress or isinstance(progress, bool):
            return None
        if isinstance(progress, (int, float)):
            # Decaid reports 0–1.
            return round(progress * 100) if progress <= 1 else round(progress)
        return None

    @property
    def release_summary(self):
        notes = _text(self.payload.get("releaseNotes"))
        return notes[:252] + "..." if notes and len(notes) > 255 else notes

    @property
    def release_url(self):
        return _text(self.payload.get("releaseUrl"))


class DecaidFirmwareUpdate(DecaidEntity, UpdateEntity):
    """The machine firmware bundled with Decaid (DE1 family only)."""

    _attr_name = "Machine firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_supported_features = UpdateEntityFeature.PROGRESS

    def __init__(self, entry):
        super().__init__(entry.runtime_data.firmware, entry, "firmware_update")

    @property
    def _recommended(self):
        artifact_id = self.payload.get("recommendedArtifactId")
        for artifact in self.payload.get("artifacts") or []:
            if isinstance(artifact, dict) and artifact.get("id") == artifact_id:
                return artifact
        return None

    @property
    def available(self):
        return super().available and self.installed_version is not None

    @property
    def installed_version(self):
        machine = self.payload.get("machine")
        build = machine.get("build") if isinstance(machine, dict) else None
        if isinstance(build, bool) or not isinstance(build, int):
            return None
        return str(build)

    @property
    def latest_version(self):
        recommended = self._recommended
        if self.payload.get("updateAvailable") is True and recommended is not None:
            # Compare builds with builds: installed_version is the build number,
            # and a label such as "v1.4" would sort below "1352".
            build = recommended.get("build")
            if isinstance(build, int) and not isinstance(build, bool):
                return str(build)
            return _text(recommended.get("versionLabel"))
        return self.installed_version

    @property
    def title(self):
        recommended = self._recommended
        if self.payload.get("updateAvailable") is True and recommended is not None:
            label = _text(recommended.get("versionLabel"))
            if label and label != self.latest_version:
                return f"Firmware {label}"
        return None

    @property
    def in_progress(self):
        operation = self.payload.get("operation")
        state = operation.get("state") if isinstance(operation, dict) else None
        return isinstance(state, str) and state not in ("idle", "")

    @property
    def release_summary(self):
        recommended = self._recommended
        notes = _text(recommended.get("releaseNotes")) if recommended else None
        return notes[:252] + "..." if notes and len(notes) > 255 else notes
