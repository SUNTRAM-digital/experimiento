# Protocolo de Desarrollo y Versionamiento

## Regla fundamental

**`master` solo recibe código que pasó todas las pruebas.**  
Ninguna fase, fix o feature toca `master` directamente. Todo entra via Pull Request con checklist completo.

---

## Estructura de ramas

```
master                    ← producción, siempre estable y probado
├── fase1-multimodel-weather  ← Fase 1: Multi-model weather engine
├── fase2-patterns            ← Fase 2: 5 patrones entry/exit (próxima)
├── fase3-backtest            ← Fase 3: Backtest engine (futura)
├── hotfix/descripcion        ← Correcciones urgentes a master
└── experiment/descripcion    ← Experimentos que pueden descartarse
```

### Naming de branches

| Tipo | Formato | Ejemplo |
|------|---------|---------|
| Nueva fase | `faseN-nombre-corto` | `fase2-patterns` |
| Corrección urgente | `hotfix/descripcion` | `hotfix/fix-noaa-timeout` |
| Experimento | `experiment/descripcion` | `experiment/ml-warming-model` |

---

## Flujo de trabajo

### 1. Empezar una nueva fase

```bash
# Siempre partir de master actualizado
git checkout master
git pull origin master

# Crear la rama
git checkout -b fase2-patterns
```

### 2. Commits atómicos durante desarrollo

Commitear cada pieza funcional por separado. **Nunca acumular cambios.**

```bash
# Formato: faseN: descripcion breve en presente
git add archivo_modificado.py
git commit -m "fase2: add 72-hour rule priority scoring"
```

### 3. Checklist antes de solicitar merge a master

Completar **TODOS** los puntos antes de abrir el PR:

#### Funcionalidad
- [ ] La feature hace lo que describe su issue/plan
- [ ] No hay `print()` de debug olvidados
- [ ] No hay claves, addresses o tokens hardcodeados
- [ ] Los nuevos parámetros tienen valores por defecto seguros en `config.py`

#### Pruebas de código
- [ ] `python -c "from nuevo_modulo import *; print('OK')"` pasa sin errores
- [ ] Los imports no rompen módulos existentes
- [ ] Los campos nuevos en dicts tienen valores de fallback (`dict.get('key', default)`)

#### Pruebas de integración (bot en modo dry-run)
- [ ] El bot arranca sin errores: `python main.py`
- [ ] El scan completa al menos 1 ciclo completo sin excepciones en logs
- [ ] La UI de Telegram responde correctamente
- [ ] No hay warnings de deprecation nuevos

#### Pruebas de regresión
- [ ] Las estrategias existentes (weather, BTC, UpDown) siguen funcionando
- [ ] `evaluate_market()` retorna el mismo formato que antes
- [ ] Los campos del dict de oportunidad que usa `claude_analyst.py` existen

#### Seguridad
- [ ] `.env` NO está en staging (`git status` no lo muestra)
- [ ] `data/state.json` NO está en staging
- [ ] No hay wallet addresses reales en el código fuente
- [ ] El `.env.example` solo tiene placeholders

### 4. Tag antes de merge

```bash
# Etiquetar el estado final de la rama antes de mergear
git tag -a vN.M -m "descripcion del estado"
# Ejemplo:
git tag -a v2.0 -m "fase2 complete: 5 patterns implemented and tested"
```

### 5. Merge a master (solo cuando TODO el checklist está completo)

```bash
git checkout master
git merge faseN-nombre --no-ff -m "merge faseN: descripcion"
git tag -a faseN-complete -m "Fase N completa y en produccion"
git push origin master
git push origin --tags
```

---

## Versioning semántico

```
v{FASE}.{SUBVERSION}

v1.0  = Fase 1 completa (multi-model weather)
v1.1  = Hotfix sobre Fase 1
v2.0  = Fase 2 completa (5 patrones)
v3.0  = Fase 3 completa (backtest engine)
```

---

## Comandos de emergencia

### El bot dejó de funcionar en master

```bash
# Ver todos los tags (versiones estables)
git tag -l

# Crear rama de hotfix desde el último tag estable
git checkout -b hotfix/descripcion v1.0

# Arreglar, probar, luego mergear de vuelta a master
```

### Una rama de desarrollo se rompió

```bash
# Ver historial de la rama
git log --oneline fase2-patterns

# Revertir solo un archivo al último commit
git checkout HEAD -- archivo_roto.py

# Volver al último commit bueno (descarta cambios posteriores)
git reset --hard {commit-hash}
```

### Deshacer el último commit (sin perder cambios)

```bash
git reset --soft HEAD~1
```

---

## Reglas de seguridad permanentes

1. **NUNCA** commitear `.env` — tiene private keys reales
2. **NUNCA** hardcodear wallet addresses en el código
3. **NUNCA** hacer `git push --force` en `master`
4. **NUNCA** usar `--no-verify` para saltarse hooks
5. **SIEMPRE** revisar `git diff --staged` antes de cada commit
6. **SIEMPRE** usar wallet dedicada con fondos mínimos para el bot
7. Si ves credenciales en el historial → notificar inmediatamente y rotar las keys

---

## Checklist rápido pre-commit

```bash
# Antes de cada commit, verificar:
git diff --staged          # ¿Qué estoy commiteando exactamente?
git status                 # ¿Hay archivos sensibles en staging?
grep -r "sk-ant" .         # ¿Hay API keys de Anthropic en el código?
grep -r "0x" . --include="*.py" | grep -v "# " | grep -v "test"  # ¿Wallet addresses?
```
