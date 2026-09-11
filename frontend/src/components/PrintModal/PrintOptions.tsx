import { useState } from 'react';
import { Settings, ChevronDown, ChevronUp } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type {
  PrintOptionsProps,
  PrintOptions as PrintOptionsType,
  CalibrationMode,
} from './types';
import {
  CALIBRATION_MODES,
  CALIBRATION_MODE_ACTIVE,
  CALIBRATION_MODE_INACTIVE,
} from '../../utils/calibrationMode';

type OptionConfig = {
  key: keyof PrintOptionsType;
  label: string;
  desc: string;
  dualNozzleOnly?: boolean;
  /** Tri-state (off/on/auto) rather than a plain on/off pair. */
  tristate?: boolean;
};

// On/off options render as the same button pair, minus the "auto" choice.
const BOOLEAN_MODES = ['off', 'on'] as const;

/**
 * Print options toggle panel with collapsible UI.
 * Shows bed levelling, flow/vibration calibration, layer inspection, timelapse,
 * and (for dual-nozzle printers only) nozzle offset calibration.
 */
export function PrintOptionsPanel({
  options,
  onChange,
  defaultExpanded = false,
  showDualNozzleOptions = false,
}: PrintOptionsProps) {
  const { t } = useTranslation();
  const [isExpanded, setIsExpanded] = useState(defaultExpanded);

  // Labels/descriptions reuse the settings.default* namespace — identical strings,
  // already translated across all locales. Only nozzle_offset_cali is new (#1682).
  const printOptionsConfig: OptionConfig[] = [
    { key: 'bed_levelling', label: t('settings.defaultBedLevelling'), desc: t('settings.defaultBedLevellingDesc'), tristate: true },
    { key: 'flow_cali', label: t('settings.defaultFlowCali'), desc: t('settings.defaultFlowCaliDesc'), tristate: true },
    { key: 'vibration_cali', label: t('settings.defaultVibrationCali'), desc: t('settings.defaultVibrationCaliDesc') },
    { key: 'layer_inspect', label: t('settings.defaultLayerInspect'), desc: t('settings.defaultLayerInspectDesc') },
    { key: 'timelapse', label: t('settings.defaultTimelapse'), desc: t('settings.defaultTimelapseDesc') },
    { key: 'nozzle_offset_cali', label: t('settings.defaultNozzleOffsetCali'), desc: t('settings.defaultNozzleOffsetCaliDesc'), dualNozzleOnly: true, tristate: true },
  ];

  const visibleOptions = printOptionsConfig.filter(o => !o.dualNozzleOnly || showDualNozzleOptions);

  const handleToggle = (key: keyof PrintOptionsType, value: boolean) => {
    onChange({ ...options, [key]: value });
  };

  const handleCalibrationMode = (key: keyof PrintOptionsType, mode: CalibrationMode) => {
    onChange({ ...options, [key]: mode });
  };

  return (
    <div className="mb-4">
      <button
        type="button"
        onClick={() => setIsExpanded(!isExpanded)}
        aria-expanded={isExpanded}
        className="flex items-center gap-2 text-sm text-bambu-gray hover:text-white transition-colors w-full"
      >
        <Settings className="w-4 h-4" />
        <span>{t('queue.bulkEdit.printOptions')}</span>
        {isExpanded ? (
          <ChevronUp className="w-4 h-4 ml-auto" />
        ) : (
          <ChevronDown className="w-4 h-4 ml-auto" />
        )}
      </button>
      {isExpanded && (
        <div className="mt-2 bg-bambu-dark rounded-lg p-3 space-y-2">
          {visibleOptions.map(({ key, label, desc, tristate }) =>
            tristate ? (
              <div key={key} className="flex items-center justify-between gap-3">
                <div>
                  <span className="text-sm text-white">{label}</span>
                  <p className="text-xs text-bambu-gray">{desc}</p>
                </div>
                <div className="flex gap-1 shrink-0">
                  {CALIBRATION_MODES.map((mode) => (
                    <button
                      key={mode}
                      type="button"
                      onClick={() => handleCalibrationMode(key, mode)}
                      className={`px-2.5 py-1 text-xs rounded transition-colors ${
                        options[key as 'bed_levelling'] === mode
                          ? CALIBRATION_MODE_ACTIVE[mode]
                          : CALIBRATION_MODE_INACTIVE
                      }`}
                    >
                      {t(`settings.calibrationMode_${mode}`)}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              <div key={key} className="flex items-center justify-between gap-3">
                <div>
                  <span className="text-sm text-white">{label}</span>
                  <p className="text-xs text-bambu-gray">{desc}</p>
                </div>
                <div className="flex gap-1 shrink-0">
                  {BOOLEAN_MODES.map((mode) => {
                    const active = (options[key as 'vibration_cali'] ? 'on' : 'off') === mode;
                    return (
                      <button
                        key={mode}
                        type="button"
                        onClick={() => handleToggle(key, mode === 'on')}
                        className={`px-2.5 py-1 text-xs rounded transition-colors ${
                          active ? CALIBRATION_MODE_ACTIVE[mode] : CALIBRATION_MODE_INACTIVE
                        }`}
                      >
                        {t(`settings.calibrationMode_${mode}`)}
                      </button>
                    );
                  })}
                </div>
              </div>
            ),
          )}

        </div>
      )}
    </div>
  );
}
