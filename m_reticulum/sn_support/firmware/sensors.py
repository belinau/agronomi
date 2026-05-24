"""µReticulum — Support Node Sensor Drivers (SN-SUPPORT-01)

MicroPython drivers for:
  - Battery voltage divider (100k/100k → GPIO1)
"""

import gc
import math
import time

import machine

# ---------------------------------------------------------------------------
# Battery voltage — ADC with 100k/100k voltage divider
# ---------------------------------------------------------------------------


def read_battery(pin, divider_ratio=2.0, samples=4):
    """Read battery voltage through a resistive divider.

    The divider scales V_bat down by *divider_ratio* so the ADC can
    safely measure it (e.g. 100k/100k divides in half).

    Args:
        pin:            GPIO number for the ADC input.
        divider_ratio:  V_bat / V_adc (default 2.0 for equal resistors).
        samples:        Number of ADC samples to average.

    Returns:
        float: Battery voltage in volts, or -1.0 on error._
    """
    try:
        adc = machine.ADC(pin)
        # ATTENUATION FIX: Changed from ATTN_DB11 to ATTN_11DB to comply with modern MicroPython releases
        adc.atten(machine.ADC.ATTN_11DB)  # 0–3.3 V range

        total = 0
        for _ in range(samples):
            total += adc.read()
            time.sleep_ms(2)

        raw = total // samples
        v_adc = (raw / 4095.0) * 3.3
        v_bat = v_adc * divider_ratio

        return round(v_bat, 2)

    except Exception as e:
        print("[SENSOR] Battery ADC error: " + str(e))
        return -1.0
