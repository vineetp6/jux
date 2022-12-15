from enum import IntEnum
from typing import Dict, NamedTuple, Tuple, Union

import jax
import jax.numpy as jnp
from jax import Array, lax
from luxai2022.config import EnvConfig as LuxEnvConfig
from luxai2022.team import Team as LuxTeam
from luxai2022.unit import Unit as LuxUnit
from luxai2022.unit import UnitType as LuxUnitType

from jux.actions import ActionQueue, UnitAction
from jux.config import EnvConfig, UnitConfig
from jux.map.position import Position
from jux.unit_cargo import ResourceType, UnitCargo
from jux.utils import INT32_MAX


class UnitType(IntEnum):
    LIGHT = 0
    HEAVY = 1

    @classmethod
    def from_lux(cls, lux_unit_type: LuxUnitType) -> "UnitType":
        if lux_unit_type == LuxUnitType.LIGHT:
            return cls.LIGHT
        elif lux_unit_type == LuxUnitType.HEAVY:
            return cls.HEAVY
        else:
            raise ValueError(f"Unknown unit type {lux_unit_type}")

    def to_lux(self) -> LuxUnitType:
        if self == self.LIGHT:
            return LuxUnitType.LIGHT
        elif self == self.HEAVY:
            return LuxUnitType.HEAVY
        else:
            raise ValueError(f"Unknown unit type {self}")


class Unit(NamedTuple):
    unit_cfg: UnitConfig
    unit_type: UnitType
    action_queue: ActionQueue  # ActionQueue[UNIT_ACTION_QUEUE_SIZE, 5]
    team_id: int = jnp.int32(INT32_MAX)
    # team # no need team object, team_id is enough
    unit_id: int = jnp.int32(INT32_MAX)
    pos: Position = Position()

    cargo: UnitCargo = UnitCargo()
    power: int = jnp.int32(0)

    @classmethod
    def new(cls, team_id: int, unit_type: Union[UnitType, int], unit_id: int, env_cfg: EnvConfig):
        unit_cfg = jax.lax.switch(unit_type, [
            lambda: env_cfg.ROBOTS[UnitType.LIGHT],
            lambda: env_cfg.ROBOTS[UnitType.HEAVY],
        ])
        return cls(
            unit_type=UnitType(unit_type) if isinstance(unit_type, int) else unit_type,
            team_id=team_id,
            unit_id=unit_id,
            pos=Position(),
            cargo=UnitCargo(),
            action_queue=ActionQueue.empty(env_cfg.UNIT_ACTION_QUEUE_SIZE),
            unit_cfg=unit_cfg,
            power=unit_cfg.INIT_POWER,
        )

    @classmethod
    def empty(cls, env_cfg: EnvConfig):
        return cls(
            unit_type=jnp.int32(UnitType.LIGHT),
            unit_cfg=env_cfg.ROBOTS[UnitType.LIGHT],
            action_queue=ActionQueue.empty(env_cfg.UNIT_ACTION_QUEUE_SIZE),
        )

    @property
    def cargo_space(self):
        return self.unit_cfg.CARGO_SPACE

    @property
    def battery_capacity(self):
        return self.unit_cfg.BATTERY_CAPACITY

    @classmethod
    def from_lux(cls, lux_unit: LuxUnit, env_cfg: EnvConfig) -> "Unit":
        unit_id = int(lux_unit.unit_id[len('unit_'):])
        return Unit(
            unit_type=jnp.int32(UnitType.from_lux(lux_unit.unit_type)),
            team_id=jnp.int32(lux_unit.team_id),
            unit_id=jnp.int32(unit_id),
            pos=Position.from_lux(lux_unit.pos),
            cargo=UnitCargo.from_lux(lux_unit.cargo),
            action_queue=ActionQueue.from_lux(lux_unit.action_queue, env_cfg.UNIT_ACTION_QUEUE_SIZE),
            unit_cfg=UnitConfig.from_lux(lux_unit.unit_cfg),
            power=lux_unit.power,
        )

    def to_lux(self, lux_teams: Dict[str, LuxTeam], lux_env_cfg: LuxEnvConfig) -> LuxUnit:
        lux_unit = LuxUnit(
            team=lux_teams[f'player_{int(self.team_id)}'],
            unit_type=UnitType(self.unit_type).to_lux(),
            unit_id=f"unit_{int(self.unit_id)}",
            env_cfg=lux_env_cfg,
        )
        lux_unit.pos = self.pos.to_lux()
        lux_unit.cargo = self.cargo.to_lux()
        lux_unit.power = int(self.power)
        lux_unit.action_queue = self.action_queue.to_lux()
        return lux_unit

    def next_action(self) -> UnitAction:
        act = self.action_queue.peek()
        act = jax.lax.cond(
            self.action_queue.is_empty(),
            lambda: UnitAction.do_nothing(),  # empty action
            lambda: act,
        )
        return act

    def repeat_action(self, success: bool) -> 'Unit':
        '''
        Currently, invalid actions in luxai2021 are not executed and also not
        removed from the action queue. So, wee need an indicator 'success' to
        indicate whether the action is executed successfully. Only when the
        action is executed successfully, we can pop/repeat it.

        Args:
            success (bool[2, U]): whether the action is executed successfully
        Returns:
            Unit: the unit with updated action queue
        '''

        def _repeat_minus_one(action_queue: ActionQueue) -> ActionQueue:
            data = action_queue.data._replace(repeat=action_queue.data.repeat.at[action_queue.front].add(-1))
            return action_queue._replace(data=data)

        def _pop_and_push_back(action_queue: ActionQueue) -> ActionQueue:
            act, new_queue = action_queue.pop()
            return new_queue.push_back(act)

        def _repeat_action(self: 'Unit'):
            act = self.action_queue.peek()
            repeat_minus_one = (act.repeat > 0)
            pop_and_push_back = (act.repeat < 0)
            action_queue = jax.lax.switch(
                repeat_minus_one + pop_and_push_back * 2,
                [
                    lambda queue: queue.pop()[1],
                    _repeat_minus_one,
                    _pop_and_push_back,
                ],
                self.action_queue,
            )
            return self._replace(action_queue=action_queue)

        return jax.lax.cond(
            success & ~self.action_queue.is_empty(),
            lambda self: _repeat_action(self),
            lambda self: self,
            self,
        )

    def is_heavy(self) -> Union[bool, Array]:
        return self.unit_type == UnitType.HEAVY

    def move_power_cost(self, rubble_at_target: int):
        return self.unit_cfg.MOVE_COST + self.unit_cfg.RUBBLE_MOVEMENT_COST * rubble_at_target

    def add_resource(self, resource: ResourceType, amount: int) -> Tuple['Unit', Union[int, Array]]:
        # If resource != ResourceType.power, call UnitCargo.add_resource.
        # else, call Unit.add_power.
        amount = jnp.maximum(amount, 0)

        def add_power(self, resource: ResourceType, amount: int):
            transfer_amount = jnp.minimum(self.battery_capacity - self.power, amount)
            new_unit = self._replace(power=self.power + transfer_amount)
            return new_unit, transfer_amount

        def add_others(self: Unit, resource: ResourceType, amount: int):
            new_cargo, transfer_amount = self.cargo.add_resource(
                resource=resource,
                amount=amount,
                cargo_space=self.cargo_space,
            )
            new_unit = self._replace(cargo=new_cargo)
            return new_unit, transfer_amount

        new_unit, transfer_amount = lax.cond(
            resource == ResourceType.power,
            add_power,
            add_others,
            *(self, resource, amount),
        )
        return new_unit, transfer_amount

    def sub_resource(self, resource: ResourceType, amount: int) -> Tuple['Unit', Union[int, Array]]:
        # If resource != ResourceType.power, call UnitCargo.add_resource.
        # else, call Unit.sub_resource.
        def sub_power(self, resource: ResourceType, amount: int):
            transfer_amount = jnp.minimum(self.power, amount)
            new_unit = self._replace(power=self.power - transfer_amount)
            return new_unit, transfer_amount

        def sub_others(self: Unit, resource: ResourceType, amount: int):
            new_cargo, transfer_amount = self.cargo.sub_resource(
                resource=resource,
                amount=amount,
            )
            new_unit = self._replace(cargo=new_cargo)
            return new_unit, transfer_amount

        new_unit, transfer_amount = lax.cond(
            resource == ResourceType.power,
            sub_power,
            sub_others,
            *(self, resource, amount),
        )
        return new_unit, transfer_amount

    def gain_power(self, power_gain_factor):
        new_power = self.power + jnp.ceil(self.unit_cfg.CHARGE * power_gain_factor).astype(jnp.int32)
        new_power = jnp.minimum(new_power, self.battery_capacity)
        return self._replace(power=new_power)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Unit):
            return False
        eq = True
        eq = eq & (self.unit_id == other.unit_id)
        eq = eq & (self.unit_type == other.unit_type)
        eq = eq & (self.action_queue == other.action_queue)
        eq = eq & (self.team_id == other.team_id)
        eq = eq & (self.unit_id == other.unit_id)
        eq = eq & (self.pos == other.pos)
        eq = eq & (self.cargo == other.cargo)
        eq = eq & (self.power == other.power)
        return eq
