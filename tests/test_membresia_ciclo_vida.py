import copy
import unittest
from datetime import date, timedelta

from tests import test_planes_membresias_pagos_b2_3 as b23


class TestCicloVidaMembresias(unittest.TestCase):
    """
    Pruebas específicas del ciclo de vida de membresías:
      - Renovación anticipada encadenada al día siguiente y clasificada como `programada` (no `activa`).
      - Solicitud `pendiente` encadenada automáticamente sin solapamiento.
      - Bloqueo 409 si se intenta aprobar un pago o actualizar vencimiento generando solapamiento.
      - Conservación de historial y pagos originales al renovar o cancelar.
      - Conteo de socios únicos con plan activo en `/api/dashboard/admin`.
      - Selección de membresía vigente hoy (y exposición separada de próxima programada) en `/api/socio/membresia` y `/api/socio/dashboard`.
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

    def test_01_renovacion_anticipada_es_programada_y_preserva_historial_y_dias(self):
        token_recep = self._make_token(self.uid_recep_1, "recep1@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            # Socio 70 tiene contrato 801 activo (inició hace 5 días, vence en 25 días)
            contrato_801_antes = copy.deepcopy(
                next(m for m in fake_db.state["membresia_socio"] if m["id_membresia_socio"] == 801)
            )
            venc_801 = date.fromisoformat(contrato_801_antes["fecha_vencimiento"])

            res = self.client.post(
                "/api/membresias/renovar",
                json={
                    "id_socio": 70,
                    "id_membresia_gimnasio": 501,
                    "estado": "activa",
                    "registrar_pago": True,
                    "metodo_pago": "efectivo",
                    "comprobante": "REN-PROG-70",
                },
                headers={"Authorization": f"Bearer {token_recep}"},
            )
            self.assertEqual(res.status_code, 201)
            nueva = res.json()["membresia_socio"]

            # Debe iniciar el día siguiente al vencimiento de 801 y figurar como PROGRAMADA (no ACTIVA)
            self.assertEqual(nueva["fecha_inicio"], (venc_801 + timedelta(days=1)).isoformat())
            self.assertEqual(nueva["fecha_vencimiento"], (venc_801 + timedelta(days=31)).isoformat())
            self.assertEqual(nueva["estado"], "programada")
            self.assertEqual(nueva["estado_efectivo"], "programada")
            self.assertTrue(nueva["es_programada"])
            self.assertFalse(nueva["es_vigente"])

            # Contrato anterior 801 intacto (historial preservado)
            contrato_801_despues = next(
                m for m in fake_db.state["membresia_socio"] if m["id_membresia_socio"] == 801
            )
            self.assertEqual(contrato_801_despues, contrato_801_antes)

            # En GET /api/membresias: 801 sigue 'activa' y la renovación aparece 'programada'
            r_list = self.client.get(
                "/api/membresias?id_socio=70",
                headers={"Authorization": f"Bearer {token_recep}"},
            )
            self.assertEqual(r_list.status_code, 200)
            by_id = {m["id_membresia_socio"]: m for m in r_list.json()["membresias"]}
            self.assertEqual(by_id[801]["estado"], "activa")
            self.assertEqual(by_id[nueva["id_membresia_socio"]]["estado"], "programada")

            # Pago de la renovación asociado exclusivamente a la nueva contratación
            pago_nuevo = res.json()["pago"]
            self.assertEqual(pago_nuevo["id_membresia_socio"], nueva["id_membresia_socio"])

    def test_02_solicitud_pendiente_con_activa_se_encadena_y_bloquea_solapamiento_al_pagar(self):
        token_dueno1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        token_socio70 = self._make_token(self.uid_socio_70, "socio70@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            venc_801 = date.fromisoformat(
                next(m for m in fake_db.state["membresia_socio"] if m["id_membresia_socio"] == 801)["fecha_vencimiento"]
            )

            # 1. Socio 70 solicita renovación pendiente enviando fecha de hoy -> backend la encadena a venc_801 + 1
            r_sol = self.client.post(
                "/api/socio/membresia/solicitar",
                json={"id_membresia_gimnasio": 501, "fecha_inicio": date.today().isoformat()},
                headers={"Authorization": f"Bearer {token_socio70}"},
            )
            self.assertEqual(r_sol.status_code, 201)
            sol = r_sol.json()["membresia_socio"]
            self.assertEqual(sol["estado"], "pendiente")
            self.assertEqual(sol["fecha_inicio"], (venc_801 + timedelta(days=1)).isoformat())

            # 2. Simular un contrato pendiente heredado con fechas SUPERPUESTAS (el bug observado en producción)
            fake_db.state["membresia_socio"].append(
                {
                    "id_membresia_socio": 899,
                    "id_socio_gimnasio": 701,
                    "id_membresia_gimnasio": 501,
                    "id_gimnasio": 10,
                    "precio": 30000.0,
                    "duracion_dias_contratados": 30,
                    "limite_asistencias_semanales": None,
                    "limite_asistencias_totales": None,
                    "fecha_inicio": date.today().isoformat(),
                    "fecha_vencimiento": (date.today() + timedelta(days=30)).isoformat(),
                    "estado": "pendiente",
                    "creado_en": "2026-11-25T10:00:00Z",
                }
            )
            # Intentar registrar un pago aprobado sobre 899 (superpuesta con 801) -> 409 Conflict
            r_pago_overlap = self.client.post(
                "/api/pagos",
                json={
                    "id_membresia_socio": 899,
                    "metodo_pago": "efectivo",
                    "estado": "aprobado",
                    "comprobante": "OVERLAP-899",
                },
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_pago_overlap.status_code, 409)

    def test_03_patch_no_permite_extender_vencimiento_superponiendo_renovacion_programada(self):
        token_dueno1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            # Crear renovación programada encadenada para Socio 70
            r_ren = self.client.post(
                "/api/membresias/renovar",
                json={"id_socio": 70, "id_membresia_gimnasio": 501, "estado": "activa"},
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_ren.status_code, 201)
            prog = r_ren.json()["membresia_socio"]
            inicio_prog = date.fromisoformat(prog["fecha_inicio"])

            # Intentar extender contrato 801 invadiendo la fecha de inicio de la renovación programada -> 409
            r_ext = self.client.patch(
                "/api/membresias/801",
                json={"fecha_vencimiento": (inicio_prog + timedelta(days=5)).isoformat()},
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_ext.status_code, 409)

            # Intentar marcar como 'activa' un contrato ya vencido (802 de Socio 71) -> 400
            r_venc = self.client.patch(
                "/api/membresias/802",
                json={"estado": "activa"},
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_venc.status_code, 400)

    def test_04_dashboard_admin_cuenta_socios_unicos_y_socio_ve_vigente_hoy_mas_programada(self):
        token_dueno1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        token_socio70 = self._make_token(self.uid_socio_70, "socio70@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            # Renovación anticipada para Socio 70 (crea fila programada) + fila heredada duplicada para Socio 70
            self.client.post(
                "/api/membresias/renovar",
                json={"id_socio": 70, "id_membresia_gimnasio": 501, "estado": "activa"},
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            # Simular incluso 2 filas activas solapadas heredadas del mismo Socio 70 (como el caso reportado)
            fake_db.state["membresia_socio"].append(
                {
                    "id_membresia_socio": 898,
                    "id_socio_gimnasio": 701,
                    "id_membresia_gimnasio": 501,
                    "id_gimnasio": 10,
                    "precio": 30000.0,
                    "duracion_dias_contratados": 90,
                    "limite_asistencias_semanales": None,
                    "limite_asistencias_totales": None,
                    "fecha_inicio": (date.today() - timedelta(days=1)).isoformat(),
                    "fecha_vencimiento": (date.today() + timedelta(days=89)).isoformat(),
                    "estado": "activa",
                    "creado_en": "2026-11-24T10:00:00Z",
                }
            )
            # Agregar tablas mínimas para dashboard_admin
            fake_db.state.setdefault("asistencia_socio", [])
            fake_db.state.setdefault("rutina_gimnasio", [])
            fake_db.state.setdefault("rutinas_asignadas_socio_gimnasio", [])

            # Dashboard admin: aunque Socio 70 tenga 2 filas 'activa' heredadas + 1 'programada',
            # "Socios con plan activo" DEBE ser 1 (único socio_gimnasio 701), no 3!
            r_dash = self.client.get(
                "/api/dashboard/admin",
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_dash.status_code, 200)
            kpis_m = r_dash.json()["kpis"]["membresias"]
            self.assertEqual(kpis_m["activas"], 1)
            self.assertEqual(kpis_m["sociosConPlanActivo"], 1)
            self.assertEqual(kpis_m["programadas"], 1)

            # Panel del socio (/api/socio/membresia): muestra la vigente HOY como principal
            # y la renovación futura aparte en `membresia_programada` (no confunde futura con actual)
            r_sm = self.client.get(
                "/api/socio/membresia",
                headers={"Authorization": f"Bearer {token_socio70}"},
            )
            self.assertEqual(r_sm.status_code, 200)
            sm_body = r_sm.json()
            self.assertEqual(sm_body["membresia"]["estado"], "activa")
            self.assertIsNotNone(sm_body["membresia_programada"])
            self.assertEqual(sm_body["membresia_programada"]["estado"], "programada")


if __name__ == "__main__":
    unittest.main()
