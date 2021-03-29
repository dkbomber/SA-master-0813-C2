from cereal import car
from common.numpy_fast import clip, interp
from selfdrive.car import apply_toyota_steer_torque_limits, create_gas_command, make_can_msg
from selfdrive.car.toyota.toyotacan import create_steer_command, create_ui_command, \
                                           create_accel_command, create_acc_cancel_command, \
                                           create_fcw_command
from selfdrive.car.toyota.values import Ecu, CAR, STATIC_MSGS, NO_STOP_TIMER_CAR, CarControllerParams, MIN_ACC_SPEED
from opendbc.can.packer import CANPacker
from common.op_params import opParams
from selfdrive.config import Conversions as CV
# from selfdrive.accel_to_gas import predict as accel_to_gas

VisualAlert = car.CarControl.HUDControl.VisualAlert


def accel_hysteresis(accel, accel_steady, enabled):

  # for small accel oscillations within ACCEL_HYST_GAP, don't change the accel command
  if not enabled:
    # send 0 when disabled, otherwise acc faults
    accel_steady = 0.
  elif accel > accel_steady + CarControllerParams.ACCEL_HYST_GAP:
    accel_steady = accel - CarControllerParams.ACCEL_HYST_GAP
  elif accel < accel_steady - CarControllerParams.ACCEL_HYST_GAP:
    accel_steady = accel + CarControllerParams.ACCEL_HYST_GAP
  accel = accel_steady

  return accel, accel_steady
op_params = opParams()

def coast_accel(speed):  # given a speed, output coasting acceleration
  points = [[0.01, op_params.get('0_coast_accel')], [.21, .425], [.3107, .535], [.431, .555],  # with no delay
            [.777, .438], [1.928, 0.265], [2.66, -0.179],
            [3.336, -0.250], [MIN_ACC_SPEED, -0.145]]
  # points = [[.0, op_params.get('0_coast_accel')], [.431, .555],  # with no delay
  #           [.777, .438], [1.928, 0.265], [2.66, -0.179],
  #           [3.336, -0.250], [MIN_ACC_SPEED, -0.145]]
  return interp(speed, *zip(*points))


class CarController():
  def __init__(self, dbc_name, CP, VM):
    self.last_steer = 0
    self.accel_steady = 0.
    self.alert_active = False
    self.last_standstill = False
    self.standstill_req = False
    self.op_params = opParams()
    self.standstill_hack = self.op_params.get('standstill_hack')

    self.steer_rate_limited = False

    self.fake_ecus = set()
    if CP.enableCamera:
      self.fake_ecus.add(Ecu.fwdCamera)
    if CP.enableDsu:
      self.fake_ecus.add(Ecu.dsu)

    self.packer = CANPacker(dbc_name)

  def compute_gb_pedal(self, accel, speed, braking):
    def accel_to_gas(a_ego, v_ego):
      speed_part = (_s1 * a_ego + _s2) * v_ego ** 2 + (_s3 * a_ego + _s4) * v_ego
      accel_part = (_a7 * v_ego + _a8) * a_ego ** 4 + (_a3 * v_ego + _a4) * a_ego ** 3 + _a5 * a_ego ** 2 + _a6 * a_ego
      ret = speed_part + accel_part + _offset
      return ret

    _a3, _a4, _a5, _a6, _a7, _a8, _s1, _s2, _s3, _s4, _offset = [-0.0034109221270790142, -0.02989942810035373, 0.002005326552420498, 0.1356381902353583, 0.0014222019070588158, 0.008436894892099946, 0.0009439048033890968, -0.0017786568461504919, 0.002986433642380856, 0.021810785976030644, -0.007020501995388009]
    coast = coast_accel(speed)
    coast_spread = self.op_params.get('coast_spread')
    gas = 0.
    if accel >= coast:
      gas = accel_to_gas(accel, speed)
      if self.op_params.get('coast_smoother'):
        if coast + coast_spread > accel:  # make sure we don't do 1/(l - l) (.16 - .16)
          x = accel - coast
          l = coast_spread
          p = 2  # controls how quickly it ramps up (higher, it waits for a longer time to ramp up gas output)
          gas *= 1 / (1 + (x / (l - x)) ** -p) if x != 0 else 0  # nice sigmoid style curve
      if braking:  # while car is braking for any reason, reduce gas output to reduce jerking and have a smoother ramp up
        gas /= 2
    return gas

    # coast_spread = self.op_params.get('coast_spread')
    # if not braking or accel - self.op_params.get('max_accel_gap') > actual_accel:  # if car not braking or gap between desired accel and actual is too high
    #   gas = accel_to_gas(accel, speed)
    #   if self.op_params.get('coast_smoother'):
    #     gas *= interp(accel, [coast, coast + coast_spread * 2], [0, 1])
    # return gas

  def update(self, enabled, CS, frame, actuators, pcm_cancel_cmd, hud_alert,
             left_line, right_line, lead, left_lane_depart, right_lane_depart):

    # *** compute control surfaces ***

    # gas and brake
    apply_gas = 0.
    apply_accel = (actuators.gas - actuators.brake) * CarControllerParams.ACCEL_SCALE
    if self.op_params.get('apply_accel') is not None and enabled:
      apply_accel = self.op_params.get('apply_accel')
      apply_gas = 0

    if CS.CP.enableGasInterceptor and enabled and CS.out.vEgo < MIN_ACC_SPEED and self.op_params.get('convert_accel_to_gas'):
      # converts desired acceleration to gas percentage for pedal
      # +0.06 offset to reduce ABS pump usage when applying very small gas
      # apply_accel *= CarControllerParams.ACCEL_SCALE
      apply_gas = self.compute_gb_pedal(apply_accel, CS.out.vEgo, CS.out.brakeLights)
      if apply_accel > 0 and CS.out.vEgo <= CS.CP.minSpeedCan:  # artifically increase accel to release brake quicker
        apply_accel *= self.op_params.get('standstill_accel_multiplier')

    # apply_accel, self.accel_steady = accel_hysteresis(apply_accel, self.accel_steady, enabled)
    apply_accel = clip(apply_accel, CarControllerParams.ACCEL_MIN, CarControllerParams.ACCEL_MAX)
    apply_gas = clip(apply_gas, 0., 1.)

    if enabled and self.op_params.get('apply_gas') is not None:
      apply_gas = self.op_params.get('apply_gas')
      apply_accel = 0


    # steer torque
    new_steer = int(round(actuators.steer * CarControllerParams.STEER_MAX))
    apply_steer = apply_toyota_steer_torque_limits(new_steer, self.last_steer, CS.out.steeringTorqueEps, CarControllerParams)
    self.steer_rate_limited = new_steer != apply_steer

    # Cut steering while we're in a known fault state (2s)
    if not enabled or CS.steer_state in [9, 25] or (abs(CS.out.steeringRateDeg) > 100 and self.op_params.get('steer_fault_fix')):
      apply_steer = 0
      apply_steer_req = 0
    else:
      apply_steer_req = 1

    if not enabled and CS.pcm_acc_status:
      # send pcm acc cancel cmd if drive is disabled but pcm is still on, or if the system can't be activated
      pcm_cancel_cmd = 1

    # on entering standstill, send standstill request
    if CS.out.standstill and not self.last_standstill and CS.CP.carFingerprint not in NO_STOP_TIMER_CAR and not self.standstill_hack:
      self.standstill_req = True
    if CS.pcm_acc_status != 8:
      # pcm entered standstill or it's disabled
      self.standstill_req = False

    self.last_steer = apply_steer
    self.last_accel = apply_accel
    self.last_standstill = CS.out.standstill

    can_sends = []

    #*** control msgs ***
    #print("steer {0} {1} {2} {3}".format(apply_steer, min_lim, max_lim, CS.steer_torque_motor)

    # toyota can trace shows this message at 42Hz, with counter adding alternatively 1 and 2;
    # sending it at 100Hz seem to allow a higher rate limit, as the rate limit seems imposed
    # on consecutive messages
    if Ecu.fwdCamera in self.fake_ecus:
      can_sends.append(create_steer_command(self.packer, apply_steer, apply_steer_req, frame))

      # LTA mode. Set ret.steerControlType = car.CarParams.SteerControlType.angle and whitelist 0x191 in the panda
      # if frame % 2 == 0:
      #   can_sends.append(create_steer_command(self.packer, 0, 0, frame // 2))
      #   can_sends.append(create_lta_steer_command(self.packer, actuators.steeringAngleDeg, apply_steer_req, frame // 2))

    # we can spam can to cancel the system even if we are using lat only control
    if (frame % 3 == 0 and CS.CP.openpilotLongitudinalControl) or (pcm_cancel_cmd and Ecu.fwdCamera in self.fake_ecus):
      lead = lead or CS.out.vEgo < 12.    # at low speed we always assume the lead is present do ACC can be engaged

      # Lexus IS uses a different cancellation message
      if pcm_cancel_cmd and CS.CP.carFingerprint == CAR.LEXUS_IS:
        can_sends.append(create_acc_cancel_command(self.packer))
      elif CS.CP.openpilotLongitudinalControl:
        can_sends.append(create_accel_command(self.packer, apply_accel, pcm_cancel_cmd, self.standstill_req, lead))
      else:
        can_sends.append(create_accel_command(self.packer, 0, pcm_cancel_cmd, False, lead))

    if (frame % 2 == 0) and (CS.CP.enableGasInterceptor):
      # send exactly zero if apply_gas is zero. Interceptor will send the max between read value and apply_gas.
      # This prevents unexpected pedal range rescaling
      can_sends.append(create_gas_command(self.packer, apply_gas, frame//2))

    # ui mesg is at 100Hz but we send asap if:
    # - there is something to display
    # - there is something to stop displaying
    fcw_alert = hud_alert == VisualAlert.fcw
    steer_alert = hud_alert == VisualAlert.steerRequired

    send_ui = False
    if ((fcw_alert or steer_alert) and not self.alert_active) or \
       (not (fcw_alert or steer_alert) and self.alert_active):
      send_ui = True
      self.alert_active = not self.alert_active
    elif pcm_cancel_cmd:
      # forcing the pcm to disengage causes a bad fault sound so play a good sound instead
      send_ui = True

    if (frame % 100 == 0 or send_ui) and Ecu.fwdCamera in self.fake_ecus:
      can_sends.append(create_ui_command(self.packer, steer_alert, pcm_cancel_cmd, left_line, right_line, left_lane_depart, right_lane_depart))

    if frame % 100 == 0 and Ecu.dsu in self.fake_ecus:
      can_sends.append(create_fcw_command(self.packer, fcw_alert))

    #*** static msgs ***

    for (addr, ecu, cars, bus, fr_step, vl) in STATIC_MSGS:
      if frame % fr_step == 0 and ecu in self.fake_ecus and CS.CP.carFingerprint in cars:
        can_sends.append(make_can_msg(addr, vl, bus))

    return can_sends
