package main

import (
	"context"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"fmt"
    "net/url"

	"github.com/joho/godotenv"
	pb "github.com/juliog922/whatsmeow_go/src/proto"
	"github.com/sirupsen/logrus"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	waLog "go.mau.fi/whatsmeow/util/log"

	grpchandler "github.com/juliog922/whatsmeow_go/src/grpc_handler"
	grpcserver "github.com/juliog922/whatsmeow_go/src/grpc_server"
	"github.com/juliog922/whatsmeow_go/src/logger"
	"github.com/lib/pq"
	//_ "github.com/mattn/go-sqlite3"
)

var (
	container *sqlstore.Container

	clientsMu sync.RWMutex
	clients   []*whatsmeow.Client                 // histórico/compatibilidad
	clientsByJID = make(map[string]*whatsmeow.Client) // JID -> client

	// listeners gRPC (igual que antes)
	streamListeners     = make(map[pb.WhatsAppService_StreamMessagesServer]struct{})
	streamListenersLock = &sync.Mutex{}

	// control de handlers por JID
	handlerIDs        = make(map[string]uint32) // JID -> handlerID
	handlerRegistered = make(map[string]bool)

	err error
)

// Interfaz mínima que necesitamos del servidor gRPC.
// Evitamos acoplar el tipo concreto (que no está exportado).
type Broadcaster interface {
	BroadcastMessage(*pb.MessageEvent)
}

func dsnFromEnv() string {
    // 1) Prioridad 1: WHATS_PG_DSN (ej: postgres://user:pass@host:5432/db?sslmode=disable)
    if v := os.Getenv("WHATS_PG_DSN"); v != "" {
        return v
    }
    // 2) Prioridad 2: DATABASE_URL (como tu servicio web)
    if v := os.Getenv("DATABASE_URL"); v != "" {
        return v
    }
    // 3) Construir desde POSTGRES_* (como en docker-compose)
    user := os.Getenv("POSTGRES_USER")
    pass := os.Getenv("POSTGRES_PASSWORD")
    db   := os.Getenv("POSTGRES_DB")
    host := os.Getenv("POSTGRES_HOST")
    port := os.Getenv("POSTGRES_PORT")
    ssl  := os.Getenv("POSTGRES_SSLMODE") // opcional

    if host == "" { host = "db" }
    if port == "" { port = "5432" }
    if ssl  == "" { ssl  = "disable" }

    // Permite cadena vacía si no hay datos suficientes; el caller pondrá un default.
    if user == "" || pass == "" || db == "" {
        return ""
    }
    return fmt.Sprintf("postgres://%s:%s@%s:%s/%s?sslmode=%s", user, pass, host, port, db, ssl)
}

func maskDSN(dsn string) string {
    // No logueamos la password
    u, err := url.Parse(dsn)
    if err != nil { return dsn }
    if u.User != nil {
        if name := u.User.Username(); name != "" {
            u.User = url.UserPassword(name, "****")
        }
    }
    return u.String()
}


func hasGrpcClients() bool {
	streamListenersLock.Lock()
	defer streamListenersLock.Unlock()
	return len(streamListeners) > 0
}

func getJID(dev *store.Device) string {
	return dev.ID.String()
}

func getClientByJID(jid string) *whatsmeow.Client {
	clientsMu.RLock()
	defer clientsMu.RUnlock()
	return clientsByJID[jid]
}

// Crea y registra un client SOLO si no existe ya para ese JID.
// Devuelve (client, created).
func addClientUnique(dev *store.Device, clientLog waLog.Logger, logger *logrus.Logger) (*whatsmeow.Client, bool) {
	jid := getJID(dev)

	clientsMu.Lock()
	defer clientsMu.Unlock()

	if existing := clientsByJID[jid]; existing != nil {
		logger.WithField("jid", jid).Warn("Client ya existe; no se crea duplicado")
		return existing, false
	}

	c := whatsmeow.NewClient(dev, clientLog)
	clients = append(clients, c)     // mantenemos compatibilidad con el slice
	clientsByJID[jid] = c
	return c, true
}

// Registra handler una sola vez por JID y guarda el handlerID.
func ensureHandler(c *whatsmeow.Client, jid string, waserver Broadcaster, logger *logrus.Logger) {
	if handlerRegistered[jid] {
		return
	}
	hid := c.AddEventHandler(grpchandler.MakeGrpcHandler(
		&grpchandler.ClientWrapper{Client: c},
		clients,
		waserver.BroadcastMessage,
		hasGrpcClients,
		logger,
	))
	handlerRegistered[jid] = true
	handlerIDs[jid] = hid // útil si alguna vez quieres RemoveEventHandler
	logger.WithField("jid", jid).Info("Event handler registrado")
}

func main() {
	_ = godotenv.Load()
	logger := logger.InitLogger()

	host := os.Getenv("WHATSMEOW_HOST")
	if host == "" {
		host = "0.0.0.0"
	}
	port := os.Getenv("WHATSMEOW_PORT")
	if port == "" {
		port = "50051"
	}

	logger.WithFields(logrus.Fields{
		"host": host,
		"port": port,
	}).Info("Starting WhatsApp gRPC service")

	ctx := context.Background()
	dbLog := waLog.Stdout("DB", "INFO", true)

	// Requerido por sqlstore + lib/pq para columnas ARRAY
	sqlstore.PostgresArrayWrapper = pq.Array

	dsn := dsnFromEnv()
	if dsn == "" {
		// Default razonable si faltan envs; apunta al servicio "db" del compose
		dsn = "postgres://postgres:postgres@db:5432/postgres?sslmode=disable"
	}

	logger.WithField("dsn", maskDSN(dsn)).Info("Connecting to Postgres…")

	container, err = sqlstore.New(ctx, "postgres", dsn, dbLog)
	if err != nil {
		logger.WithError(err).Fatal("Failed to initialize SQL store (Postgres)")
	}
	defer container.Close()


	// Inicializar el servidor gRPC
	waserver := grpcserver.StartGRPC(
		host,
		port,
		container,
		&clients,
		&streamListeners,
		streamListenersLock,
		logger,
		hasGrpcClients,
	)

	clientLog := waLog.Stdout("Client", "INFO", true)

	// Intentar cargar y conectar dispositivos existentes
	// Cargar y conectar dispositivos existentes
	devices, err := container.GetAllDevices(ctx)
	if err != nil {
		logger.WithError(err).Error("Failed to load devices from store")
		devices = []*store.Device{}
	}

	if len(devices) == 0 {
		logger.Warn("No devices found. Service will wait for login via StartLogin.")
	} else {
		logger.WithField("count", len(devices)).Info("Devices found. Proceeding to connect.")

		// 1) deduplicar por JID (por si el store trae duplicados)
		uniq := make(map[string]*store.Device)
		for _, dev := range devices {
			j := getJID(dev)
			if _, ok := uniq[j]; ok {
				logger.WithField("jid", j).Warn("Device duplicado en store; se usará el primero")
				continue
			}
			uniq[j] = dev
		}

		// 2) crear/registrar clientes únicos + handler único + conectar
		for jidStr, dev := range uniq {
			c, created := addClientUnique(dev, clientLog, logger)
			if created {
				ensureHandler(c, jidStr, waserver, logger) // 'waserver' implementa Broadcaster
			} else {
				logger.WithField("jid", jidStr).Warn("Saltando creación de client duplicado")
			}

			if err := c.Connect(); err != nil {
				logger.WithFields(logrus.Fields{"jid": jidStr}).
					WithError(err).Error("Failed to connect device")

				// limpiar sesión inválida
				if err.Error() == "server responded with 401" ||
					err.Error() == "got 401: logged out from another device connect failure" ||
					err.Error() == "failed to send usync query: websocket not connected" {

					logger.WithField("jid", jidStr).Warn("Removing invalid session from store")
					_ = container.DeleteDevice(ctx, dev)

					// opcional: si se registró handler, quitarlo
					if hid, ok := handlerIDs[jidStr]; ok {
						go c.RemoveEventHandler(hid) // no bloquea el lock interno
					}
					clientsMu.Lock()
					delete(clientsByJID, jidStr)
					clientsMu.Unlock()
				}
				continue
			}

			_ = c.FetchAppState(ctx, appstate.WAPatchCriticalUnblockLow, true, false)
			logger.WithField("jid", jidStr).Info("Device connected")
		}
	}

	logger.Info("Service is ready. Waiting for messages or QR logins.")


	// Señal de apagado
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	<-sig

	logger.Warn("Interrupt received. Disconnecting clients...")
	for _, c := range clients {
		c.Disconnect()
	}
	logger.Info("Shutdown complete.")
}
