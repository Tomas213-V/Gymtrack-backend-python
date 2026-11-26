import copy
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from app.routers import dashboard_socio as dashboard_socio_router
from tests import test_planes_membresias_pagos_b2_3 as b23


class TestCicloVidaMembresias(unittest.TestCase):
    """
    Pruebas específicas del ciclo de vida de membresías (reemplazo inmediato con traspaso de días):
      - Sin membresía programada: al contratar/renovar una membresía estando otra activa,
        se cancela lógicamente la anterior (preservando historial y pagos) y sus días restantes
        de uso se suman al vencimiento de la nueva membresía que inicia HOY.
      - Ejemplo exacto del usuario: plan 30 días con 27 pasados (3 restantes) + nuevo plan 60 días ->
        inicia HOY y vence en 63 días (`60 + 3`), anterior queda 'cancelada'.
      - Solicitud `pendiente` inicia HOY sin cancelar todavía la activa; al aprobarse el pago
        se cancela la anterior y se transfieren sus días restantes.
      - Conteo de socios únicos con plan activo en `/api/dashboard/admin` y `membresia_programada = None`.
    """

    @classmethod
    def setUpClass(cls):
        b23.TestPlanesMembresiasPagosB23.setUpClass()
        cls.fixture = b23.TestPlanesMembresiasPagosB23()
        cls.client = b23.TestPlanesMembresiasPagosB23.client
        cls.uid_dueno_1 = b23.TestPlanesMembresiasPagosB23.uid_dueno_1
        cls.uid_recep_1 = b23.TestPlanesMembresiasPagosB23.uid_recep_1
        cls.uid_socio_70 = b23.TestPlanesMembresiasPagosB23.uid_socio_70

    def _make_token(self, sub: str, email: str):
        return self.fixture._make_token(sub, email)

    def _patch_env(self, **kwargs):
        return self.fixture._patch_env(**kwargs)

    def test_01_contratar_nueva_cancela_anterior_y_suma_dias_restantes_exactos(self):
        token_recep = self._make_token(self.uid_recep_1, "recep1@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            hoy = date.today()
            # Configurar el ejemplo exacto: plan 30 días donde ya pasaron 27 -> quedan 3 días
            contrato_801 = next(m for m in fake_db.state["membresia_socio"] if m["id_membresia_socio"] == 801)
            contrato_801["fecha_inicio"] = (hoy - timedelta(days=27)).isoformat()
            contrato_801["fecha_vencimiento"] = (hoy + timedelta(days=3)).isoformat()
            contrato_801["estado"] = "activa"

            # Agregar plan de 2 meses (60 días) en Gym 10
            fake_db.state["membresia_gimnasio"].append(
                {
                    "id_membresia_gimnasio": 599,
                    "nombre": "Plan Bimestral 60d",
                    "descripcion": "2 meses",
                    "precio": 55000.0,
                    "duracion_dias": 60,
                    "limite_asistencias_semanales": None,
                    "limite_asistencias_totales": None,
                    "estado": True,
                    "id_gimnasio": 10,
                }
            )

            res = self.client.post(
                "/api/membresias/renovar",
                json={
                    "id_socio": 70,
                    "id_membresia_gimnasio": 599,
                    "estado": "activa",
                    "registrar_pago": True,
                    "metodo_pago": "efectivo",
                    "comprobante": "REN-60D-MAS-3D",
                },
                headers={"Authorization": f"Bearer {token_recep}"},
            )
            self.assertEqual(res.status_code, 201, res.text)
            body = res.json()
            nueva = body["membresia_socio"]

            # Inicia HOY, activa de inmediato (sin programada), y vence en 60 + 3 = 63 días
            self.assertEqual(nueva["fecha_inicio"], hoy.isoformat())
            self.assertEqual(nueva["fecha_vencimiento"], (hoy + timedelta(days=63)).isoformat())
            self.assertEqual(nueva["estado"], "activa")
            self.assertEqual(nueva["estado_efectivo"], "activa")
            self.assertFalse(nueva["es_programada"])
            self.assertTrue(nueva["es_vigente"])
            self.assertEqual(body["dias_transferidos"], 3)
            self.assertEqual(body["membresias_previas_canceladas"], [801])

            # Contrato anterior 801 cancelado lógicamente (fila histórica y condiciones originales preservadas)
            contrato_801_despues = next(
                m for m in fake_db.state["membresia_socio"] if m["id_membresia_socio"] == 801
            )
            self.assertEqual(contrato_801_despues["estado"], "cancelada")
            self.assertEqual(contrato_801_despues["fecha_inicio"], (hoy - timedelta(days=27)).isoformat())
            self.assertEqual(contrato_801_despues["precio"], 30000.0)

            # Pago de la renovación asociado exclusivamente a la nueva contratación
            pago_nuevo = body["pago"]
            self.assertEqual(pago_nuevo["id_membresia_socio"], nueva["id_membresia_socio"])

    def test_02_solicitud_pendiente_no_cancela_activa_hasta_aprobar_pago_y_suma_dias(self):
        token_dueno1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        token_socio70 = self._make_token(self.uid_socio_70, "socio70@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            hoy = date.today()
            contrato_801 = next(m for m in fake_db.state["membresia_socio"] if m["id_membresia_socio"] == 801)
            contrato_801["fecha_inicio"] = (hoy - timedelta(days=25)).isoformat()
            contrato_801["fecha_vencimiento"] = (hoy + timedelta(days=5)).isoformat()

            # 1. Socio 70 solicita nuevo plan pendiente -> inicia HOY, y 801 sigue activa mientras no se pague
            r_sol = self.client.post(
                "/api/socio/membresia/solicitar",
                json={"id_membresia_gimnasio": 501, "fecha_inicio": hoy.isoformat()},
                headers={"Authorization": f"Bearer {token_socio70}"},
            )
            self.assertEqual(r_sol.status_code, 201, r_sol.text)
            sol = r_sol.json()["membresia_socio"]
            self.assertEqual(sol["estado"], "pendiente")
            self.assertEqual(sol["fecha_inicio"], hoy.isoformat())
            self.assertEqual(contrato_801["estado"], "activa")

            # 2. Al aprobar el pago de la solicitud pendiente -> cancela 801 y suma sus 5 días restantes (30 + 5 = 35)
            r_pago = self.client.post(
                "/api/pagos",
                json={
                    "id_membresia_socio": sol["id_membresia_socio"],
                    "metodo_pago": "efectivo",
                    "estado": "aprobado",
                    "comprobante": "APROB-TRASPASO-5D",
                },
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_pago.status_code, 201, r_pago.text)
            self.assertEqual(r_pago.json()["dias_transferidos"], 5)
            self.assertEqual(r_pago.json()["membresias_previas_canceladas"], [801])

            contrato_801_post = next(m for m in fake_db.state["membresia_socio"] if m["id_membresia_socio"] == 801)
            sol_post = next(
                m for m in fake_db.state["membresia_socio"] if m["id_membresia_socio"] == sol["id_membresia_socio"]
            )
            self.assertEqual(contrato_801_post["estado"], "cancelada")
            self.assertEqual(sol_post["estado"], "activa")
            self.assertEqual(sol_post["fecha_vencimiento"], (hoy + timedelta(days=35)).isoformat())

    def test_03_patch_activa_reemplaza_y_rechaza_activar_contrato_vencido(self):
        token_dueno1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            hoy = date.today()
            # Simular contrato pendiente nuevo para Socio 70 mientras 801 tiene 4 días restantes
            contrato_801 = next(m for m in fake_db.state["membresia_socio"] if m["id_membresia_socio"] == 801)
            contrato_801["fecha_inicio"] = (hoy - timedelta(days=26)).isoformat()
            contrato_801["fecha_vencimiento"] = (hoy + timedelta(days=4)).isoformat()

            fake_db.state["membresia_socio"].append(
                {
                    "id_membresia_socio": 890,
                    "id_socio_gimnasio": 701,
                    "id_membresia_gimnasio": 501,
                    "id_gimnasio": 10,
                    "precio": 30000.0,
                    "duracion_dias_contratados": 30,
                    "limite_asistencias_semanales": None,
                    "limite_asistencias_totales": None,
                    "fecha_inicio": hoy.isoformat(),
                    "fecha_vencimiento": (hoy + timedelta(days=30)).isoformat(),
                    "estado": "pendiente",
                    "creado_en": "2026-11-25T10:00:00Z",
                }
            )
            # Activar 890 con pago automático -> cancela 801 y extiende vencimiento a hoy + 34 días
            r_act = self.client.patch(
                "/api/membresias/890",
                json={"estado": "activa", "registrar_pago_si_falta": True},
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_act.status_code, 200, r_act.text)
            self.assertEqual(r_act.json()["dias_transferidos"], 4)
            self.assertEqual(r_act.json()["membresias_previas_canceladas"], [801])
            self.assertEqual(r_act.json()["membresia_socio"]["fecha_vencimiento"], (hoy + timedelta(days=34)).isoformat())

            # Intentar marcar como 'activa' un contrato ya vencido (802 de Socio 71) -> 400
            r_venc = self.client.patch(
                "/api/membresias/802",
                json={"estado": "activa"},
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_venc.status_code, 400)

    def test_04_dashboard_admin_y_socio_sin_programadas_y_contrato_futuro_heredado_solo_transfiere_dias_pagados(self):
        token_dueno1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        token_socio70 = self._make_token(self.uid_socio_70, "socio70@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            hoy = date.today()
            # Simular un contrato heredado futuro (como #10: 2027-11-26 a 2027-12-26 -> 30 días reales)
            contrato_801 = next(m for m in fake_db.state["membresia_socio"] if m["id_membresia_socio"] == 801)
            contrato_801["fecha_inicio"] = (hoy + timedelta(days=366)).isoformat()
            contrato_801["fecha_vencimiento"] = (hoy + timedelta(days=396)).isoformat()

            # Al contratar nueva de 30 días, cancela la futura heredada y suma exactamente sus 30 días (no 396)
            r_ren = self.client.post(
                "/api/membresias/renovar",
                json={"id_socio": 70, "id_membresia_gimnasio": 501, "estado": "activa"},
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_ren.status_code, 201, r_ren.text)
            self.assertEqual(r_ren.json()["dias_transferidos"], 30)
            self.assertEqual(r_ren.json()["membresia_socio"]["fecha_inicio"], hoy.isoformat())
            self.assertEqual(r_ren.json()["membresia_socio"]["fecha_vencimiento"], (hoy + timedelta(days=60)).isoformat())

            fake_db.state.setdefault("asistencia_socio", [])
            fake_db.state.setdefault("rutina_gimnasio", [])
            fake_db.state.setdefault("rutinas_asignadas_socio_gimnasio", [])

            r_dash = self.client.get(
                "/api/dashboard/admin",
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_dash.status_code, 200)
            kpis_m = r_dash.json()["kpis"]["membresias"]
            self.assertEqual(kpis_m["activas"], 1)
            self.assertEqual(kpis_m["sociosConPlanActivo"], 1)
            self.assertEqual(kpis_m["programadas"], 0)

            r_sm = self.client.get(
                "/api/socio/membresia",
                headers={"Authorization": f"Bearer {token_socio70}"},
            )
            self.assertEqual(r_sm.status_code, 200)
            sm_body = r_sm.json()
            self.assertEqual(sm_body["membresia"]["estado"], "activa")
            self.assertIsNone(sm_body["membresia_programada"])

    def test_05_simular_pago_mercadopago_socio_con_y_sin_service_role(self):
        token_socio70 = self._make_token(self.uid_socio_70, "socio70@gymtrack.test")
        dashboard_socio_router.reset_demo_simulated_payments_store()

        # Caso A: con service_role habilitado en servidor -> persiste en BD via RPCs oficiales
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2, (
            patch("app.config.has_service_role", return_value=True)
        ), (
            patch("app.config.get_supabase_admin_client", return_value=fake_db)
        ):
            hoy = date.today()
            contrato_801 = next(m for m in fake_db.state["membresia_socio"] if m["id_membresia_socio"] == 801)
            contrato_801["fecha_inicio"] = (hoy - timedelta(days=25)).isoformat()
            contrato_801["fecha_vencimiento"] = (hoy + timedelta(days=5)).isoformat()
            contrato_801["estado"] = "activa"

            res_sim_bd = self.client.post(
                "/api/socio/pagos/simular-mercadopago",
                json={"id_membresia_gimnasio": 501},
                headers={"Authorization": f"Bearer {token_socio70}"},
            )
            self.assertEqual(res_sim_bd.status_code, 201, res_sim_bd.text)
            body_bd = res_sim_bd.json()
            self.assertTrue(body_bd["persistido_en_bd"])
            self.assertEqual(body_bd["modo"], "mercadopago_simulado_bd")
            self.assertEqual(body_bd["dias_transferidos"], 5)
            self.assertEqual(body_bd["membresias_previas_canceladas"], [801])
            self.assertEqual(body_bd["membresia_socio"]["estado"], "activa")
            self.assertEqual(body_bd["membresia_socio"]["fecha_vencimiento"], (hoy + timedelta(days=35)).isoformat())
            self.assertEqual(body_bd["pago"]["metodo_pago"], "mercadopago")
            self.assertEqual(body_bd["pago"]["estado"], "aprobado")

        # Caso B: sin service_role local (modo presentacion) -> refleja membresia activa y pago MP en panel socio Y paneles del dueno
        token_dueno1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        p1b, p2b, fake_db_b, _ = self._patch_env()
        with p1b, p2b, patch("app.config.has_service_role", return_value=False):
            hoy = date.today()
            contrato_801_b = next(m for m in fake_db_b.state["membresia_socio"] if m["id_membresia_socio"] == 801)
            contrato_801_b["fecha_inicio"] = (hoy - timedelta(days=28)).isoformat()
            contrato_801_b["fecha_vencimiento"] = (hoy + timedelta(days=2)).isoformat()
            contrato_801_b["estado"] = "activa"
            fake_db_b.state.setdefault("asistencia_socio", [])
            fake_db_b.state.setdefault("rutina_socio", [])
            fake_db_b.state.setdefault("rutinas_asignadas_socio_gimnasio", [])

            res_sim_demo = self.client.post(
                "/api/socio/pagos/simular-mercadopago",
                json={"id_membresia_gimnasio": 501},
                headers={"Authorization": f"Bearer {token_socio70}"},
            )
            self.assertEqual(res_sim_demo.status_code, 201, res_sim_demo.text)
            body_demo = res_sim_demo.json()
            self.assertFalse(body_demo["persistido_en_bd"])
            self.assertEqual(body_demo["modo"], "mercadopago_simulado_presentacion")
            self.assertEqual(body_demo["dias_transferidos"], 2)
            self.assertEqual(body_demo["membresia_socio"]["fecha_vencimiento"], (hoy + timedelta(days=32)).isoformat())
            self.assertEqual(body_demo["pago"]["metodo_pago"], "mercadopago")
            self.assertEqual(body_demo["pago"]["estado"], "aprobado")

            r_sm = self.client.get(
                "/api/socio/membresia",
                headers={"Authorization": f"Bearer {token_socio70}"},
            )
            self.assertEqual(r_sm.status_code, 200)
            sm_json = r_sm.json()
            self.assertEqual(sm_json["membresia"]["estado"], "activa")
            self.assertEqual(sm_json["membresia"]["fecha_vencimiento"], (hoy + timedelta(days=32)).isoformat())
            self.assertEqual(sm_json["pagos"][0]["metodo_pago"], "mercadopago")
            self.assertEqual(sm_json["pagos"][0]["estado"], "aprobado")

            r_dash = self.client.get(
                "/api/socio/dashboard",
                headers={"Authorization": f"Bearer {token_socio70}"},
            )
            self.assertEqual(r_dash.status_code, 200)
            self.assertEqual(r_dash.json()["membresia"]["estado"], "activa")
            self.assertEqual(r_dash.json()["ultimo_pago"]["metodo_pago"], "mercadopago")

            # Verificar visibilidad en los paneles del dueno: /api/membresias, /api/pagos, /api/pagos/resumen, /api/dashboard/admin y /api/socios
            r_membs_dueno = self.client.get(
                "/api/membresias",
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_membs_dueno.status_code, 200, r_membs_dueno.text)
            sim_ms_id = body_demo["membresia_socio"]["id_membresia_socio"]
            m_dueno = next(
                (m for m in r_membs_dueno.json()["membresias"] if m["id_membresia_socio"] == sim_ms_id),
                None,
            )
            self.assertIsNotNone(m_dueno)
            self.assertEqual(m_dueno["estado_efectivo"], "activa")

            r_pagos_dueno = self.client.get(
                "/api/pagos",
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_pagos_dueno.status_code, 200, r_pagos_dueno.text)
            sim_pago_id = body_demo["pago"]["id_pago"]
            p_dueno = next(
                (p for p in r_pagos_dueno.json()["datos"] if p["id_pago"] == sim_pago_id),
                None,
            )
            self.assertIsNotNone(p_dueno)
            self.assertEqual(p_dueno["metodo_pago"], "mercadopago")
            self.assertEqual(p_dueno["estado"], "aprobado")

            r_res_pagos = self.client.get(
                "/api/pagos/resumen",
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_res_pagos.status_code, 200)
            self.assertGreaterEqual(r_res_pagos.json()["pagos_aprobados"], 1)

            r_dash_dueno = self.client.get(
                "/api/dashboard/admin",
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_dash_dueno.status_code, 200)
            self.assertTrue(
                any(
                    p["id_pago"] == sim_pago_id
                    for p in r_dash_dueno.json()["actividadReciente"]["pagos"]
                )
            )
            self.assertGreaterEqual(r_dash_dueno.json()["kpis"]["ingresos"]["pagosAprobados"], 2)

            r_socios_dueno = self.client.get(
                "/api/socios",
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_socios_dueno.status_code, 200)
            soc_70 = next(s for s in r_socios_dueno.json()["socios"] if s["id_socio"] == 70)
            self.assertEqual(soc_70["membresia_actual"]["id_membresia_socio"], sim_ms_id)

        dashboard_socio_router.reset_demo_simulated_payments_store()


if __name__ == "__main__":
    unittest.main()
